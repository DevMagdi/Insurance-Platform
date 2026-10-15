-- Affected staff receive a private notification. No grant is placed on the
-- credential/role tables; all changes below run as guarded SECURITY DEFINER.
CREATE FUNCTION app_security.notify_access_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    INSERT INTO public.notifications(recipient_staff_id, kind, payload)
        VALUES (NEW.target_user_id, 'access_changed',
            jsonb_build_object('change_type', NEW.change_type,
                               'changed_at', NEW.created_at));
    RETURN NEW;
END;
$function$;
CREATE TRIGGER tr_access_changed AFTER INSERT ON access_change_log
    FOR EACH ROW EXECUTE FUNCTION app_security.notify_access_change();

CREATE FUNCTION app_security.explain_access(p_target uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_snapshot jsonb; v_explanation jsonb;
BEGIN
    IF NOT app_security.has_permission('users.view')
       OR NOT app_security.has_global_scope() THEN RETURN NULL; END IF;
    v_snapshot := app_security.access_snapshot(p_target);
    IF v_snapshot IS NULL THEN RETURN NULL; END IF;
    SELECT COALESCE(jsonb_object_agg(p.code, jsonb_build_object(
        'roles', COALESCE((SELECT jsonb_agg(r.name ORDER BY r.name)
             FROM public.user_roles ur JOIN public.roles r ON r.id = ur.role_id
             JOIN public.role_permissions rp ON rp.role_id = r.id
             WHERE ur.user_id = p_target AND rp.permission_code = p.code), '[]'::jsonb),
        'override', (SELECT o.effect::text FROM public.user_permission_overrides o
             WHERE o.user_id = p_target AND o.permission_code = p.code),
        'effective', (v_snapshot->'permissions') ? p.code
    )), '{}'::jsonb) INTO v_explanation FROM public.permissions p;
    RETURN jsonb_build_object('access', v_snapshot, 'why', v_explanation,
        'editable', p_target <> app_security.actor_id()
             AND app_security.has_permission('users.manage_access'));
END;
$function$;

CREATE FUNCTION app_security.save_custom_role(
    p_role uuid, p_name text, p_description text, p_codes text[])
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_before jsonb; v_after jsonb; v_user uuid;
BEGIN
    IF NOT app_security.has_permission('roles.manage')
       OR NOT app_security.has_global_scope()
       OR length(btrim(p_name)) NOT BETWEEN 2 AND 80
       OR length(btrim(p_description)) > 300 OR p_codes IS NULL
       OR cardinality(p_codes) > 40
       OR p_name IN ('administrator','supervisor','agent','analyst')
       OR EXISTS (SELECT 1 FROM unnest(p_codes) AS code
            WHERE code IS NULL OR NOT app_security.has_permission(code)) THEN
        RAISE EXCEPTION 'role change forbidden' USING ERRCODE = '42501';
    END IF;
    IF p_role IS NULL THEN
        INSERT INTO public.roles(name, description, is_system)
            VALUES (btrim(p_name), btrim(p_description), false)
            RETURNING id INTO v_id;
        INSERT INTO public.audit_logs(actor_staff_id, action, resource_type,
            resource_id, after_state)
            VALUES (app_security.actor_id(), 'role_created', 'role', v_id,
                jsonb_build_object('name', p_name, 'permissions', to_jsonb(p_codes)));
    ELSE
        SELECT r.id INTO v_id FROM public.roles r WHERE r.id = p_role
            AND NOT r.is_system AND r.name NOT IN
                ('administrator','supervisor','agent','analyst') FOR UPDATE;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'system role protected or role absent' USING ERRCODE = '42501';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('insurance-platform:last-administrator'));
        SELECT jsonb_agg(rp.permission_code ORDER BY rp.permission_code)
            INTO v_before FROM public.role_permissions rp WHERE rp.role_id = v_id;
        UPDATE public.roles SET name = btrim(p_name), description = btrim(p_description)
            WHERE id = v_id;
        DELETE FROM public.role_permissions WHERE role_id = v_id;
    END IF;
    INSERT INTO public.role_permissions (role_id, permission_code)
        SELECT v_id, code FROM (SELECT DISTINCT unnest(p_codes) AS code) grants;
    IF p_role IS NOT NULL THEN
        SELECT jsonb_agg(rp.permission_code ORDER BY rp.permission_code)
            INTO v_after FROM public.role_permissions rp WHERE rp.role_id = v_id;
        FOR v_user IN SELECT ur.user_id FROM public.user_roles ur WHERE ur.role_id = v_id
        LOOP
            UPDATE public.staff_users SET token_version = token_version + 1,
                updated_at = now() WHERE id = v_user;
            UPDATE public.auth_sessions SET revoked_at = now()
                WHERE subject_kind = 'staff' AND subject_id = v_user
                    AND revoked_at IS NULL;
            INSERT INTO public.access_change_log (actor_staff_id, target_user_id,
                change_type, before_state, after_state)
                VALUES (app_security.actor_id(), v_user, 'role_changed',
                    jsonb_build_object('role_id', v_id, 'permissions',
                        COALESCE(v_before, '[]'::jsonb)),
                    jsonb_build_object('role_id', v_id, 'permissions',
                        COALESCE(v_after, '[]'::jsonb)));
        END LOOP;
        INSERT INTO public.audit_logs(actor_staff_id, action, resource_type,
            resource_id, before_state, after_state)
            VALUES (app_security.actor_id(), 'role_updated', 'role', v_id,
                jsonb_build_object('permissions', COALESCE(v_before, '[]'::jsonb)),
                jsonb_build_object('permissions', COALESCE(v_after, '[]'::jsonb)));
    END IF;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.clone_custom_role(p_source uuid, p_name text)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_codes text[]; v_desc text;
BEGIN
    SELECT array_agg(rp.permission_code) FILTER (WHERE rp.permission_code IS NOT NULL),
        r.description INTO v_codes, v_desc
        FROM public.roles r LEFT JOIN public.role_permissions rp ON rp.role_id = r.id
        WHERE r.id = p_source GROUP BY r.id;
    IF NOT FOUND THEN RETURN NULL; END IF;
    RETURN app_security.save_custom_role(NULL, p_name,
        'Clone of ' || left(v_desc, 100), COALESCE(v_codes, ARRAY[]::text[]));
END;
$function$;

CREATE FUNCTION app_security.roles_catalog() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('roles.manage')
            AND app_security.has_global_scope() THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', r.id, 'name', r.name, 'description', r.description,
            'is_system', r.is_system,
            'permissions', COALESCE((SELECT jsonb_agg(rp.permission_code ORDER BY rp.permission_code)
                FROM public.role_permissions rp WHERE rp.role_id = r.id), '[]'::jsonb)
        ) ORDER BY r.name) FROM public.roles r), '[]'::jsonb)
        ELSE NULL END
$function$;

CREATE FUNCTION app_security.edit_staff_profile(
    p_target uuid, p_email text, p_name text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_before jsonb;
BEGIN
    IF NOT app_security.has_permission('users.edit')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id()
       OR p_email <> lower(btrim(p_email))
       OR p_email !~ '^[^@ ]+@[^@ ]+\.[^@ ]+$'
       OR length(btrim(p_name)) NOT BETWEEN 2 AND 120 THEN RETURN false; END IF;
    SELECT jsonb_build_object('email', email, 'full_name', full_name)
        INTO v_before FROM public.staff_users WHERE id = p_target FOR UPDATE;
    IF v_before IS NULL THEN RETURN false; END IF;
    UPDATE public.staff_users SET email = p_email, full_name = btrim(p_name),
        token_version = token_version + 1, updated_at = now()
        WHERE id = p_target;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    INSERT INTO public.access_change_log(actor_staff_id, target_user_id,
        change_type, before_state, after_state)
        VALUES (app_security.actor_id(), p_target, 'profile_updated', v_before,
            jsonb_build_object('email', p_email, 'full_name', p_name));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.reactivate_staff(p_target uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.has_permission('users.edit')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id() THEN
        RETURN false;
    END IF;
    UPDATE public.staff_users SET status = 'active', token_version = token_version + 1,
        updated_at = now() WHERE id = p_target AND status = 'disabled'
        AND password_hash IS NOT NULL;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    INSERT INTO public.access_change_log(actor_staff_id, target_user_id,
        change_type, after_state)
        VALUES (app_security.actor_id(), p_target, 'reactivated',
            jsonb_build_object('status', 'active'));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.force_staff_logout(p_target uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.has_permission('users.manage_access')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id() THEN
        RETURN false;
    END IF;
    UPDATE public.staff_users SET token_version = token_version + 1,
        updated_at = now() WHERE id = p_target;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    INSERT INTO public.access_change_log(actor_staff_id, target_user_id,
        change_type, after_state)
        VALUES (app_security.actor_id(), p_target, 'force_logout',
            jsonb_build_object('sessions_revoked', true));
    RETURN true;
END;
$function$;

-- Active users reset passwords using the same one-use 48-hour token protocol
-- as first-time invitations, without exposing/storing the token plaintext.
CREATE FUNCTION app_security.issue_staff_password_reset(
    p_target uuid, p_digest bytea, p_expires timestamptz)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_email text; v_admin boolean;
BEGIN
    IF NOT app_security.has_permission('users.edit')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id()
       OR octet_length(p_digest) <> 32 OR p_expires <= now()
       OR p_expires > now() + interval '3 days' THEN
        RAISE EXCEPTION 'password reset forbidden' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('insurance-platform:last-administrator'));
    SELECT EXISTS (SELECT 1 FROM public.user_roles ur JOIN public.roles r ON r.id = ur.role_id
        WHERE ur.user_id = p_target AND r.name = 'administrator') INTO v_admin;
    IF v_admin AND (SELECT count(DISTINCT ur.user_id) FROM public.user_roles ur
        JOIN public.roles r ON r.id = ur.role_id
        JOIN public.staff_users u ON u.id = ur.user_id
        WHERE r.name = 'administrator' AND u.status = 'active') <= 1 THEN
        RAISE EXCEPTION 'cannot lock out last administrator' USING ERRCODE = '23514';
    END IF;
    SELECT email INTO v_email FROM public.staff_users
        WHERE id = p_target AND status = 'active' FOR UPDATE;
    IF v_email IS NULL THEN RETURN NULL; END IF;
    UPDATE public.staff_users SET password_hash = NULL,
        must_change_password = true, token_version = token_version + 1,
        updated_at = now() WHERE id = p_target;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    UPDATE public.staff_invitations SET consumed_at = now()
        WHERE staff_user_id = p_target AND consumed_at IS NULL;
    INSERT INTO public.staff_invitations
        (staff_user_id, token_digest, created_by, expires_at)
        VALUES (p_target, p_digest, app_security.actor_id(), p_expires);
    INSERT INTO public.access_change_log(actor_staff_id, target_user_id,
        change_type, after_state)
        VALUES (app_security.actor_id(), p_target, 'password_reset_requested',
            jsonb_build_object('sessions_revoked', true));
    RETURN v_email;
END;
$function$;

CREATE OR REPLACE FUNCTION app_security.accept_staff_invitation(
    p_digest bytea, p_password_hash text) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_invite staff_invitations%ROWTYPE; v_status staff_status;
BEGIN
    IF app_security.actor_scope() <> 'invitation' OR octet_length(p_digest) <> 32
       OR length(p_password_hash) < 40 THEN RETURN NULL; END IF;
    SELECT * INTO v_invite FROM public.staff_invitations
        WHERE token_digest = p_digest FOR UPDATE;
    IF NOT FOUND OR v_invite.consumed_at IS NOT NULL OR v_invite.expires_at <= now() THEN
        RETURN NULL;
    END IF;
    SELECT u.status INTO v_status FROM public.staff_users u
        WHERE u.id = v_invite.staff_user_id FOR UPDATE;
    IF v_status = 'invited' OR (v_status = 'active' AND EXISTS
        (SELECT 1 FROM public.staff_users u WHERE u.id = v_invite.staff_user_id
         AND u.must_change_password AND u.password_hash IS NULL)) THEN
        UPDATE public.staff_users SET password_hash = p_password_hash,
            status = 'active', must_change_password = false,
            token_version = token_version + 1, updated_at = now()
            WHERE id = v_invite.staff_user_id;
        UPDATE public.auth_sessions SET revoked_at = now()
            WHERE subject_kind = 'staff' AND subject_id = v_invite.staff_user_id
              AND revoked_at IS NULL;
        UPDATE public.staff_invitations SET consumed_at = now() WHERE id = v_invite.id;
        RETURN v_invite.staff_user_id;
    END IF;
    RETURN NULL;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.explain_access(uuid),
    app_security.save_custom_role(uuid, text, text, text[]),
    app_security.clone_custom_role(uuid, text),
    app_security.roles_catalog(),
    app_security.edit_staff_profile(uuid, text, text),
    app_security.reactivate_staff(uuid),
    app_security.force_staff_logout(uuid),
    app_security.issue_staff_password_reset(uuid, bytea, timestamptz)
    TO app_runtime;
