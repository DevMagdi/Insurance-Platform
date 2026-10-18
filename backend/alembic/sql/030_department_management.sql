-- Expand-only department metadata. Departments describe an employee's workplace;
-- they never grant access to complaints, teams, roles or company data.
ALTER TABLE public.departments
    ADD COLUMN description text NOT NULL DEFAULT '',
    ADD COLUMN is_active boolean NOT NULL DEFAULT true;
ALTER TABLE public.departments
    ADD CONSTRAINT department_description_length CHECK (length(btrim(description)) <= 500) NOT VALID;
ALTER TABLE public.departments VALIDATE CONSTRAINT department_description_length;

CREATE OR REPLACE FUNCTION app_security.staff_departments() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('users.view') AND app_security.has_global_scope()
        THEN COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', d.id, 'name', d.name, 'description', d.description,
            'is_active', d.is_active, 'created_at', d.created_at,
            'employee_count', (SELECT count(*) FROM public.staff_users u
                WHERE u.department_id = d.id))
            ORDER BY d.is_active DESC, d.name) FROM public.departments d), '[]'::jsonb)
        ELSE NULL END
$function$;

CREATE FUNCTION app_security.create_department(
    p_name text, p_description text, p_is_active boolean) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('users.edit') OR NOT app_security.has_global_scope()
       OR p_name IS NULL OR length(btrim(p_name)) NOT BETWEEN 2 AND 120
       OR p_description IS NULL OR length(btrim(p_description)) > 500
       OR p_is_active IS NULL THEN RETURN NULL; END IF;
    INSERT INTO public.departments(name, description, is_active)
        VALUES (btrim(p_name), btrim(p_description), p_is_active) RETURNING id INTO v_id;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id, after_state)
        VALUES (app_security.actor_id(), 'department_created', 'department', v_id,
            jsonb_build_object('name', btrim(p_name), 'description', btrim(p_description),
                'is_active', p_is_active));
    RETURN v_id;
END;
$function$;

-- Old API pods can still call the name-only signature during a rolling deploy.
-- The same permission and global-scope checks run inside the new overload.
CREATE OR REPLACE FUNCTION app_security.create_department(p_name text) RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.create_department(p_name, '', true)
$function$;

CREATE FUNCTION app_security.update_department(
    p_id uuid, p_name text, p_description text, p_is_active boolean) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_before public.departments%ROWTYPE;
BEGIN
    IF NOT app_security.has_permission('users.edit') OR NOT app_security.has_global_scope()
       OR p_id IS NULL OR p_name IS NULL OR length(btrim(p_name)) NOT BETWEEN 2 AND 120
       OR p_description IS NULL OR length(btrim(p_description)) > 500
       OR p_is_active IS NULL THEN RETURN false; END IF;
    SELECT * INTO v_before FROM public.departments WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.departments SET name = btrim(p_name),
        description = btrim(p_description), is_active = p_is_active WHERE id = p_id;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        before_state, after_state)
        VALUES (app_security.actor_id(), 'department_updated', 'department', p_id,
            jsonb_build_object('name', v_before.name, 'description', v_before.description,
                'is_active', v_before.is_active),
            jsonb_build_object('name', btrim(p_name), 'description', btrim(p_description),
                'is_active', p_is_active));
    RETURN true;
END;
$function$;

-- An inactive department remains visible to existing employees, but cannot be
-- selected for a new invitation or assignment (including by an older API pod).
CREATE OR REPLACE FUNCTION app_security.set_staff_department(
    p_user uuid, p_department uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_before uuid;
BEGIN
    IF NOT app_security.has_permission('users.edit') OR NOT app_security.has_global_scope()
       OR NOT EXISTS (SELECT 1 FROM public.departments d
            WHERE d.id = p_department AND d.is_active) THEN RETURN false; END IF;
    SELECT u.department_id INTO v_before FROM public.staff_users u WHERE u.id = p_user FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.staff_users SET department_id = p_department, updated_at = now() WHERE id = p_user;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        before_state, after_state) VALUES (app_security.actor_id(), 'department_changed',
        'staff', p_user, jsonb_build_object('department_id', v_before),
        jsonb_build_object('department_id', p_department));
    RETURN true;
END;
$function$;

CREATE OR REPLACE FUNCTION app_security.create_staff_invitation_in_department(
    p_email text, p_name text, p_digest bytea, p_expires timestamptz, p_department uuid) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF p_department IS NULL OR NOT EXISTS (SELECT 1 FROM public.departments
        WHERE id = p_department AND is_active) THEN
        RAISE EXCEPTION 'invalid department' USING ERRCODE = '22023';
    END IF;
    v_id := app_security.create_staff_invitation(p_email, p_name, p_digest, p_expires);
    UPDATE public.staff_users SET department_id = p_department WHERE id = v_id;
    RETURN v_id;
END;
$function$;

-- Keep the existing TABLE return type to support rolling deployments.
CREATE OR REPLACE FUNCTION app_security.staff_profile(p_user uuid)
RETURNS TABLE (metadata jsonb, personal_ciphertext bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT jsonb_build_object('id', u.id, 'email', u.email, 'full_name', u.full_name,
        'status', u.status, 'department_id', d.id, 'department_name', d.name,
        'department_description', d.description, 'department_is_active', d.is_active,
        'created_at', u.created_at, 'last_login_at', u.last_login_at,
        'two_factor_enabled', u.two_factor_enabled,
        'two_factor_required', u.two_factor_required,
        'roles', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', r.id, 'name', r.name, 'description', r.description,
            'is_system', r.is_system) ORDER BY r.name)
            FROM public.user_roles ur JOIN public.roles r ON r.id = ur.role_id
            WHERE ur.user_id = u.id), '[]'::jsonb),
        'permissions', COALESCE(app_security.access_snapshot(u.id)->'permissions', '[]'::jsonb),
        'profile_completed_at', p.completed_at,
        'national_id_masked', CASE WHEN p.staff_user_id IS NOT NULL
            THEN '**********' || p.national_id_last4 ELSE NULL END,
        'has_avatar', p.staff_user_id IS NOT NULL), p.personal_ciphertext
    FROM public.staff_users u JOIN public.departments d ON d.id = u.department_id
        LEFT JOIN public.staff_profiles p ON p.staff_user_id = u.id
    WHERE u.id = p_user AND app_security.has_permission('users.view')
        AND app_security.has_global_scope()
$function$;

REVOKE ALL ON FUNCTION app_security.create_department(text, text, boolean),
    app_security.update_department(uuid, text, text, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.create_department(text, text, boolean),
    app_security.update_department(uuid, text, text, boolean) TO app_runtime;
