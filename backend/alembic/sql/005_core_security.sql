-- Authentication and authorization helpers only return the minimum fields needed
-- by the API. These functions are owned by the trusted BYPASSRLS migration role.
CREATE FUNCTION app_security.staff_credentials(p_email text)
RETURNS TABLE (staff_id uuid, email text, full_name text, password_hash text,
    status staff_status, must_change_password boolean, two_factor_enabled boolean,
    two_factor_required boolean, totp_secret_ciphertext bytea, token_version integer,
    global_2fa_required boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, u.email, u.full_name, u.password_hash, u.status,
        u.must_change_password, u.two_factor_enabled, u.two_factor_required,
        u.totp_secret_ciphertext, u.token_version, s.staff_2fa_required
    FROM public.staff_users u CROSS JOIN public.security_settings s
    WHERE app_security.actor_scope() = 'staff_auth'
      AND u.email = lower(btrim(p_email)) AND s.singleton
$function$;

CREATE FUNCTION app_security.staff_credentials_by_id(p_id uuid)
RETURNS TABLE (staff_id uuid, email text, full_name text, password_hash text,
    status staff_status, must_change_password boolean, two_factor_enabled boolean,
    two_factor_required boolean, totp_secret_ciphertext bytea, token_version integer,
    global_2fa_required boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, u.email, u.full_name, u.password_hash, u.status,
        u.must_change_password, u.two_factor_enabled, u.two_factor_required,
        u.totp_secret_ciphertext, u.token_version, s.staff_2fa_required
    FROM public.staff_users u CROSS JOIN public.security_settings s
    WHERE app_security.actor_scope() = 'staff_auth' AND u.id = p_id AND s.singleton
$function$;

CREATE FUNCTION app_security.customer_by_mobile(p_mobile text, p_blind bytea DEFAULT NULL)
RETURNS TABLE (customer_id uuid, token_version integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT c.id, c.token_version FROM public.customers c
    WHERE app_security.actor_scope() = 'customer_auth' AND c.mobile = p_mobile
      AND (p_blind IS NULL OR c.national_id_blind_index = p_blind)
    LIMIT 3
$function$;

CREATE FUNCTION app_security.register_customer(
    p_full_name text, p_mobile text, p_ciphertext bytea, p_blind bytea,
    p_last4 varchar(4), p_email text DEFAULT NULL, p_whatsapp boolean DEFAULT false)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'customer_auth' OR length(btrim(p_full_name)) < 2
       OR p_mobile !~ '^\+201[0-9]{9}$' OR octet_length(p_blind) <> 32
       OR octet_length(p_ciphertext) < 28 OR p_last4 !~ '^[0-9]{4}$' THEN
        RAISE EXCEPTION 'invalid customer registration' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.customers (full_name, mobile, national_id_ciphertext,
        national_id_blind_index, national_id_last4, email, whatsapp_available)
    VALUES (btrim(p_full_name), p_mobile, p_ciphertext, p_blind,
        p_last4, nullif(btrim(p_email), ''), p_whatsapp) RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.verify_customer_identity(p_mobile text, p_blind bytea)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.actor_scope() = 'customer' AND EXISTS (
        SELECT 1 FROM public.customers c WHERE c.id = app_security.actor_id()
            AND c.mobile = p_mobile AND c.national_id_blind_index = p_blind)
$function$;

CREATE FUNCTION app_security.current_subject_version() RETURNS integer
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_version integer;
BEGIN
    IF app_security.actor_scope() = 'staff' THEN
        SELECT token_version INTO v_version FROM public.staff_users
        WHERE id = app_security.actor_id() AND status = 'active';
    ELSIF app_security.actor_scope() = 'customer' THEN
        SELECT token_version INTO v_version FROM public.customers
        WHERE id = app_security.actor_id();
    END IF;
    RETURN v_version;
END;
$function$;

CREATE FUNCTION app_security.access_snapshot(p_user_id uuid DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_user uuid := coalesce(p_user_id, app_security.actor_id()); v_result jsonb;
BEGIN
    IF NOT app_security.is_active_staff() OR
       (v_user <> app_security.actor_id() AND NOT
           (app_security.has_permission('users.view') AND app_security.has_global_scope())) THEN
        RETURN NULL;
    END IF;
    SELECT jsonb_build_object(
        'id', u.id, 'email', u.email, 'full_name', u.full_name,
        'status', u.status::text, 'token_version', u.token_version,
        'two_factor_enabled', u.two_factor_enabled,
        'two_factor_required', u.two_factor_required,
        'roles', COALESCE((SELECT jsonb_agg(ur.role_id::text) FROM public.user_roles ur
            WHERE ur.user_id = u.id), '[]'::jsonb),
        'permissions', COALESCE((SELECT jsonb_agg(p.code ORDER BY p.code)
            FROM public.permissions p WHERE
            (EXISTS (SELECT 1 FROM public.user_roles ur JOIN public.role_permissions rp
                ON rp.role_id = ur.role_id WHERE ur.user_id = u.id
                AND rp.permission_code = p.code)
             OR EXISTS (SELECT 1 FROM public.user_permission_overrides o
                WHERE o.user_id = u.id AND o.permission_code = p.code AND o.effect = 'allow'))
            AND NOT EXISTS (SELECT 1 FROM public.user_permission_overrides o
                WHERE o.user_id = u.id AND o.permission_code = p.code AND o.effect = 'deny')
        ), '[]'::jsonb),
        'overrides', COALESCE((SELECT jsonb_object_agg(o.permission_code, o.effect::text)
            FROM public.user_permission_overrides o WHERE o.user_id = u.id), '{}'::jsonb),
        'all_companies', EXISTS (SELECT 1 FROM public.user_company_scope s
            WHERE s.user_id = u.id AND s.all_companies),
        'company_ids', COALESCE((SELECT jsonb_agg(s.company_id::text)
            FROM public.user_company_scope s WHERE s.user_id = u.id
            AND NOT s.all_companies), '[]'::jsonb),
        'insurance_types', COALESCE((SELECT jsonb_agg(t.insurance_type::text)
            FROM public.user_insurance_type_scope t WHERE t.user_id = u.id), '[]'::jsonb),
        'ownership', (SELECT o.level::text FROM public.user_ownership_level o
            WHERE o.user_id = u.id),
        'team_ids', COALESCE((SELECT jsonb_agg(tm.team_id::text)
            FROM public.team_members tm WHERE tm.user_id = u.id), '[]'::jsonb)
    ) INTO v_result FROM public.staff_users u WHERE u.id = v_user;
    RETURN v_result;
END;
$function$;

CREATE FUNCTION app_security.staff_directory()
RETURNS TABLE (id uuid, email text, full_name text, status staff_status,
    two_factor_enabled boolean, two_factor_required boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, u.email, u.full_name, u.status, u.two_factor_enabled,
        u.two_factor_required FROM public.staff_users u
    WHERE app_security.has_permission('users.view') AND app_security.has_global_scope()
    ORDER BY u.email
$function$;

CREATE FUNCTION app_security.create_staff_invitation(
    p_email text, p_full_name text, p_digest bytea, p_expires timestamptz)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('users.create') OR NOT app_security.has_global_scope()
       OR p_email <> lower(btrim(p_email)) OR p_email !~ '^[^@ ]+@[^@ ]+\.[^@ ]+$'
       OR length(btrim(p_full_name)) < 2 OR octet_length(p_digest) <> 32
       OR p_expires <= now() OR p_expires > now() + interval '3 days' THEN
        RAISE EXCEPTION 'invitation not permitted' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.staff_users (email, full_name, status)
        VALUES (p_email, btrim(p_full_name), 'invited') RETURNING id INTO v_id;
    INSERT INTO public.staff_invitations
        (staff_user_id, token_digest, created_by, expires_at)
        VALUES (v_id, p_digest, app_security.actor_id(), p_expires);
    INSERT INTO public.access_change_log
        (actor_staff_id, target_user_id, change_type, after_state)
        VALUES (app_security.actor_id(), v_id, 'invited', jsonb_build_object('email', p_email));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.accept_staff_invitation(p_digest bytea, p_password_hash text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_invite staff_invitations%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'invitation' OR octet_length(p_digest) <> 32
       OR length(p_password_hash) < 40 THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_invite FROM public.staff_invitations
        WHERE token_digest = p_digest FOR UPDATE;
    IF NOT FOUND OR v_invite.consumed_at IS NOT NULL OR v_invite.expires_at <= now()
       OR NOT EXISTS (SELECT 1 FROM public.staff_users
            WHERE id = v_invite.staff_user_id AND status = 'invited') THEN
        RETURN NULL;
    END IF;
    UPDATE public.staff_users SET password_hash = p_password_hash, status = 'active',
        must_change_password = false, token_version = token_version + 1,
        updated_at = now() WHERE id = v_invite.staff_user_id;
    UPDATE public.staff_invitations SET consumed_at = now() WHERE id = v_invite.id;
    RETURN v_invite.staff_user_id;
END;
$function$;

CREATE FUNCTION app_security.change_own_password(p_password_hash text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.is_active_staff() OR length(p_password_hash) < 40 THEN
        RETURN false;
    END IF;
    UPDATE public.staff_users SET password_hash = p_password_hash,
        must_change_password = false, token_version = token_version + 1,
        updated_at = now() WHERE id = app_security.actor_id();
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = app_security.actor_id()
        AND revoked_at IS NULL;
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type, resource_id)
        VALUES (app_security.actor_id(), 'password_changed', 'staff_user', app_security.actor_id());
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.set_own_totp(p_ciphertext bytea) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.is_active_staff() OR octet_length(p_ciphertext) < 28 THEN
        RETURN false;
    END IF;
    UPDATE public.staff_users SET totp_secret_ciphertext = p_ciphertext,
        two_factor_enabled = true, token_version = token_version + 1,
        updated_at = now() WHERE id = app_security.actor_id();
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = app_security.actor_id()
        AND revoked_at IS NULL;
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type, resource_id)
        VALUES (app_security.actor_id(), 'totp_enabled', 'staff_user', app_security.actor_id());
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.record_staff_login() RETURNS void
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    UPDATE public.staff_users SET last_login_at = now()
    WHERE id = app_security.actor_id() AND app_security.is_active_staff()
$function$;

-- FORCE RLS is deliberate even on the auxiliary tables: they are not a way
-- around the complaint/customer parent policies. No raw invitation or staff
-- password table SELECT is granted to the runtime role.
ALTER TABLE auth_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY auth_sessions_owner ON auth_sessions FOR ALL TO app_runtime
    USING (subject_id = app_security.actor_id()
        AND subject_kind = app_security.actor_scope()
        AND (subject_kind = 'customer' OR app_security.is_active_staff()))
    WITH CHECK (subject_id = app_security.actor_id()
        AND subject_kind = app_security.actor_scope()
        AND (subject_kind = 'customer' OR app_security.is_active_staff()));
ALTER TABLE staff_invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_invitations FORCE ROW LEVEL SECURITY;
ALTER TABLE security_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE security_settings FORCE ROW LEVEL SECURITY;
ALTER TABLE complaint_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaint_idempotency FORCE ROW LEVEL SECURITY;
CREATE POLICY idempotency_customer ON complaint_idempotency FOR ALL TO app_runtime
    USING (app_security.actor_scope() = 'customer'
        AND customer_id = app_security.actor_id())
    WITH CHECK (app_security.actor_scope() = 'customer'
        AND customer_id = app_security.actor_id()
        AND (complaint_id IS NULL OR
            app_security.can_read_complaint_id(complaint_id, complaint_created_at)));
ALTER TABLE attachment_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachment_uploads FORCE ROW LEVEL SECURITY;
CREATE POLICY attachment_uploads_actor ON attachment_uploads FOR ALL TO app_runtime
    USING (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
        AND ((app_security.actor_scope() = 'customer'
                AND uploaded_by_customer_id = app_security.actor_id())
             OR (app_security.actor_scope() = 'staff'
                AND uploaded_by_staff_id = app_security.actor_id()
                AND app_security.has_permission('complaints.view_attachments'))))
    WITH CHECK (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
        AND ((app_security.actor_scope() = 'customer'
                AND uploaded_by_customer_id = app_security.actor_id())
             OR (app_security.actor_scope() = 'staff'
                AND uploaded_by_staff_id = app_security.actor_id()
                AND app_security.has_permission('complaints.view_attachments'))));
ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_outbox FORCE ROW LEVEL SECURITY;

GRANT SELECT, INSERT, UPDATE (revoked_at, last_used_at) ON auth_sessions TO app_runtime;
GRANT SELECT, INSERT, UPDATE (complaint_id, complaint_created_at, ticket_no)
    ON complaint_idempotency TO app_runtime;
GRANT SELECT, INSERT, UPDATE (status, finalized_at) ON attachment_uploads TO app_runtime;
GRANT SELECT (supported_types) ON insurance_companies TO app_runtime;
GRANT SELECT (national_id_last4, token_version) ON customers TO app_runtime;
GRANT SELECT (upload_id) ON attachments TO app_runtime;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app_security TO app_runtime;
