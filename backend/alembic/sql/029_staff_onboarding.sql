-- Expand-only staff onboarding. Departments are organizational metadata, NOT
-- authorization teams: changing a department never grants complaint access.
CREATE TABLE public.departments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE CHECK (length(btrim(name)) BETWEEN 2 AND 120),
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.departments(id, name)
    VALUES ('d5c4c2f7-2c1d-4f45-b9d6-366866c90ec9', 'الإدارة العامة');
-- The constant default covers existing staff and old bootstrap/seed binaries.
ALTER TABLE public.staff_users ADD COLUMN department_id uuid NOT NULL
    DEFAULT 'd5c4c2f7-2c1d-4f45-b9d6-366866c90ec9';
ALTER TABLE public.staff_users ADD CONSTRAINT fk_staff_department
    FOREIGN KEY (department_id) REFERENCES public.departments(id) NOT VALID;
ALTER TABLE public.staff_users VALIDATE CONSTRAINT fk_staff_department;

CREATE TABLE public.staff_profiles (
    staff_user_id uuid PRIMARY KEY REFERENCES public.staff_users(id) ON DELETE CASCADE,
    personal_ciphertext bytea NOT NULL CHECK (octet_length(personal_ciphertext) BETWEEN 32 AND 16384),
    national_id_blind_index bytea NOT NULL UNIQUE CHECK (octet_length(national_id_blind_index) = 32),
    national_id_last4 text NOT NULL CHECK (national_id_last4 ~ '^[0-9]{4}$'),
    -- Only decoded, resized, metadata-free JPEGs are encrypted by the application.
    avatar_content bytea NOT NULL CHECK (octet_length(avatar_content) BETWEEN 32 AND 524319),
    completed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.departments FORCE ROW LEVEL SECURITY;
ALTER TABLE public.staff_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_profiles FORCE ROW LEVEL SECURITY;
REVOKE ALL ON public.departments, public.staff_profiles FROM PUBLIC, app_runtime, app_worker;

CREATE FUNCTION app_security.staff_departments() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('users.view') AND app_security.has_global_scope()
        THEN COALESCE((SELECT jsonb_agg(jsonb_build_object('id', d.id, 'name', d.name)
            ORDER BY d.name) FROM public.departments d), '[]'::jsonb) ELSE NULL END
$function$;

CREATE FUNCTION app_security.create_department(p_name text) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('users.edit') OR NOT app_security.has_global_scope()
       OR p_name IS NULL OR length(btrim(p_name)) NOT BETWEEN 2 AND 120 THEN RETURN NULL; END IF;
    INSERT INTO public.departments(name) VALUES (btrim(p_name)) RETURNING id INTO v_id;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id, after_state)
        VALUES (app_security.actor_id(), 'department_created', 'department', v_id,
            jsonb_build_object('name', btrim(p_name)));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.set_staff_department(p_user uuid, p_department uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_before uuid;
BEGIN
    IF NOT app_security.has_permission('users.edit') OR NOT app_security.has_global_scope()
       OR NOT EXISTS (SELECT 1 FROM public.departments d WHERE d.id = p_department) THEN
        RETURN false;
    END IF;
    SELECT u.department_id INTO v_before FROM public.staff_users u WHERE u.id = p_user FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.staff_users SET department_id = p_department, updated_at = now() WHERE id = p_user;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        before_state, after_state) VALUES (app_security.actor_id(), 'department_changed', 'staff',
        p_user, jsonb_build_object('department_id', v_before),
        jsonb_build_object('department_id', p_department));
    RETURN true;
END;
$function$;

-- Preserve the original directory's return type for rolling deployments.
CREATE FUNCTION app_security.staff_directory_profiles()
RETURNS TABLE (id uuid, email text, full_name text, status staff_status,
    two_factor_enabled boolean, two_factor_required boolean, last_login_at timestamptz,
    department_id uuid, department_name text, has_avatar boolean, profile_completed_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, u.email, u.full_name, u.status, u.two_factor_enabled, u.two_factor_required,
        u.last_login_at, d.id, d.name, p.staff_user_id IS NOT NULL, p.completed_at
    FROM public.staff_users u JOIN public.departments d ON d.id = u.department_id
        LEFT JOIN public.staff_profiles p ON p.staff_user_id = u.id
    WHERE app_security.has_permission('users.view') AND app_security.has_global_scope()
    ORDER BY u.email
$function$;

CREATE FUNCTION app_security.staff_profile(p_user uuid)
RETURNS TABLE (metadata jsonb, personal_ciphertext bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT jsonb_build_object('id', u.id, 'email', u.email, 'full_name', u.full_name,
        'status', u.status, 'department_id', d.id, 'department_name', d.name,
        'created_at', u.created_at, 'profile_completed_at', p.completed_at,
        'national_id_masked', CASE WHEN p.staff_user_id IS NOT NULL
            THEN '**********' || p.national_id_last4 ELSE NULL END,
        'has_avatar', p.staff_user_id IS NOT NULL), p.personal_ciphertext
    FROM public.staff_users u JOIN public.departments d ON d.id = u.department_id
        LEFT JOIN public.staff_profiles p ON p.staff_user_id = u.id
    WHERE u.id = p_user AND app_security.has_permission('users.view')
        AND app_security.has_global_scope()
$function$;

CREATE FUNCTION app_security.staff_avatar(p_user uuid) RETURNS bytea
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT p.avatar_content FROM public.staff_profiles p
    WHERE p.staff_user_id = p_user AND app_security.has_permission('users.view')
        AND app_security.has_global_scope()
$function$;

CREATE FUNCTION app_security.create_staff_invitation_in_department(
    p_email text, p_name text, p_digest bytea, p_expires timestamptz, p_department uuid) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF p_department IS NULL OR NOT EXISTS (SELECT 1 FROM public.departments WHERE id = p_department)
        THEN RAISE EXCEPTION 'invalid department' USING ERRCODE = '22023'; END IF;
    v_id := app_security.create_staff_invitation(p_email, p_name, p_digest, p_expires);
    UPDATE public.staff_users SET department_id = p_department WHERE id = v_id;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.staff_invitation_details(p_digest bytea) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT jsonb_build_object('id', u.id, 'email', u.email, 'full_name', u.full_name,
        'department_name', d.name, 'kind', CASE WHEN u.status = 'invited' THEN 'invitation'
            ELSE 'password_reset' END)
    FROM public.staff_invitations i JOIN public.staff_users u ON u.id = i.staff_user_id
        JOIN public.departments d ON d.id = u.department_id
    WHERE app_security.actor_scope() = 'invitation' AND octet_length(p_digest) = 32
        AND i.token_digest = p_digest AND i.consumed_at IS NULL AND i.expires_at > now()
        AND (u.status = 'invited' OR (u.status = 'active' AND u.must_change_password
            AND u.password_hash IS NULL))
$function$;

-- Old password-only acceptance can still reset a password, but cannot activate
-- an invited account without the mandatory profile. This closes the legacy API bypass.
CREATE OR REPLACE FUNCTION app_security.accept_staff_invitation(p_digest bytea, p_password_hash text)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_invite public.staff_invitations%ROWTYPE; v_user public.staff_users%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'invitation' OR p_digest IS NULL
       OR octet_length(p_digest) <> 32 OR p_password_hash IS NULL
       OR length(p_password_hash) < 40 THEN RETURN NULL; END IF;
    SELECT * INTO v_invite FROM public.staff_invitations WHERE token_digest = p_digest FOR UPDATE;
    IF NOT FOUND OR v_invite.consumed_at IS NOT NULL OR v_invite.expires_at <= now() THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_user FROM public.staff_users WHERE id = v_invite.staff_user_id FOR UPDATE;
    IF (v_user.status = 'invited' AND EXISTS
            (SELECT 1 FROM public.staff_profiles p WHERE p.staff_user_id = v_user.id))
       OR (v_user.status = 'active' AND v_user.must_change_password AND v_user.password_hash IS NULL)
    THEN
        UPDATE public.staff_users SET password_hash = p_password_hash, status = 'active',
            must_change_password = false, token_version = token_version + 1, updated_at = now()
            WHERE id = v_user.id;
        UPDATE public.auth_sessions SET revoked_at = now()
            WHERE subject_kind = 'staff' AND subject_id = v_user.id AND revoked_at IS NULL;
        UPDATE public.staff_invitations SET consumed_at = now() WHERE id = v_invite.id;
        RETURN v_user.id;
    END IF;
    RETURN NULL;
END;
$function$;

CREATE FUNCTION app_security.complete_staff_invitation(p_digest bytea, p_user uuid,
    p_password_hash text, p_name text, p_personal bytea, p_blind bytea, p_last4 text, p_avatar bytea)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_invite public.staff_invitations%ROWTYPE; v_status staff_status; v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'invitation' OR p_password_hash IS NULL
       OR length(p_password_hash) < 40 THEN RETURN NULL; END IF;
    SELECT * INTO v_invite FROM public.staff_invitations WHERE token_digest = p_digest FOR UPDATE;
    IF NOT FOUND OR v_invite.consumed_at IS NOT NULL OR v_invite.expires_at <= now()
       OR v_invite.staff_user_id <> p_user THEN RETURN NULL; END IF;
    SELECT u.status INTO v_status FROM public.staff_users u WHERE u.id = p_user FOR UPDATE;
    IF v_status = 'invited' THEN
        IF p_name IS NULL OR length(btrim(p_name)) NOT BETWEEN 2 AND 120
           OR p_personal IS NULL OR p_blind IS NULL OR p_last4 IS NULL OR p_avatar IS NULL
           THEN RETURN NULL; END IF;
        INSERT INTO public.staff_profiles(staff_user_id, personal_ciphertext,
            national_id_blind_index, national_id_last4, avatar_content)
            VALUES (p_user, p_personal, p_blind, p_last4, p_avatar);
        UPDATE public.staff_users SET full_name = btrim(p_name) WHERE id = p_user;
    ELSIF v_status <> 'active' OR p_personal IS NOT NULL OR p_avatar IS NOT NULL THEN
        RETURN NULL;
    END IF;
    v_id := app_security.accept_staff_invitation(p_digest, p_password_hash);
    IF v_id IS NULL THEN RAISE EXCEPTION 'invitation unavailable' USING ERRCODE = '22023'; END IF;
    -- Audit contains no national ID, phone, address, photo or bearer token.
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id, after_state)
        VALUES (NULL, CASE WHEN v_status = 'invited' THEN 'staff_onboarding_completed'
            ELSE 'staff_password_reset' END, 'staff', v_id, jsonb_build_object('completed', true));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.reissue_staff_invitation(p_user uuid, p_digest bytea, p_expires timestamptz)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_email text;
BEGIN
    IF NOT app_security.has_permission('users.create') OR NOT app_security.has_global_scope()
       OR p_digest IS NULL OR octet_length(p_digest) <> 32 OR p_expires IS NULL
       OR p_expires <= now() OR p_expires > now() + interval '3 days' THEN RETURN NULL; END IF;
    SELECT u.email INTO v_email FROM public.staff_users u WHERE u.id = p_user AND u.status = 'invited'
        FOR UPDATE;
    IF NOT FOUND THEN RETURN NULL; END IF;
    UPDATE public.staff_invitations SET consumed_at = now()
        WHERE staff_user_id = p_user AND consumed_at IS NULL;
    INSERT INTO public.staff_invitations(staff_user_id, token_digest, created_by, expires_at)
        VALUES (p_user, p_digest, app_security.actor_id(), p_expires);
    RETURN v_email;
END;
$function$;

-- Account invitations use the enabled WORKPLACE email connection, never a
-- company-specific integration or sandbox_recipient. Inviting staff need no
-- integration-management permission; encrypted credentials never leave the API.
CREATE FUNCTION app_security.staff_email_connection()
RETURNS TABLE (provider text, config_ciphertext bytea, connection_status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT i.provider, i.config_ciphertext, i.connection_status
    FROM public.integrations_config i WHERE i.company_id IS NULL AND i.is_enabled
        AND i.provider IN ('smtp','sendgrid','ses','gmail_oauth','outlook_oauth')
        AND i.config_ciphertext IS NOT NULL AND app_security.has_global_scope()
        AND (app_security.has_permission('users.create') OR app_security.has_permission('users.edit'))
    ORDER BY (i.connection_status = 'connected') DESC, i.updated_at DESC, i.id LIMIT 1
$function$;

REVOKE ALL ON FUNCTION app_security.staff_departments(), app_security.create_department(text),
    app_security.set_staff_department(uuid, uuid), app_security.staff_directory_profiles(),
    app_security.staff_profile(uuid), app_security.staff_avatar(uuid),
    app_security.create_staff_invitation_in_department(text, text, bytea, timestamptz, uuid),
    app_security.staff_invitation_details(bytea),
    app_security.complete_staff_invitation(bytea, uuid, text, text, bytea, bytea, text, bytea),
    app_security.reissue_staff_invitation(uuid, bytea, timestamptz),
    app_security.staff_email_connection() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.staff_departments(), app_security.create_department(text),
    app_security.set_staff_department(uuid, uuid), app_security.staff_directory_profiles(),
    app_security.staff_profile(uuid), app_security.staff_avatar(uuid),
    app_security.create_staff_invitation_in_department(text, text, bytea, timestamptz, uuid),
    app_security.staff_invitation_details(bytea),
    app_security.complete_staff_invitation(bytea, uuid, text, text, bytea, bytea, text, bytea),
    app_security.reissue_staff_invitation(uuid, bytea, timestamptz),
    app_security.staff_email_connection() TO app_runtime;
