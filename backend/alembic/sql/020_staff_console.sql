-- Phase 7 staff-console support (revision 020). New reads and writes remain narrow,
-- permission-gated SECURITY DEFINER calls; app_runtime receives no table grants.
INSERT INTO public.permissions (code, resource, action, description)
    VALUES ('teams.manage', 'teams', 'manage', 'Manage teams and membership')
    ON CONFLICT (code) DO NOTHING;
INSERT INTO public.role_permissions (role_id, permission_code)
    SELECT id, 'teams.manage' FROM public.roles WHERE name = 'administrator'
    ON CONFLICT DO NOTHING;

-- Staff directory is safe metadata only; password hashes and TOTP secrets are
-- still inaccessible. Last-login is exposed only through the global admin gate.
DROP FUNCTION app_security.staff_directory();
CREATE FUNCTION app_security.staff_directory()
RETURNS TABLE (id uuid, email text, full_name text, status staff_status,
    two_factor_enabled boolean, two_factor_required boolean, last_login_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, u.email, u.full_name, u.status, u.two_factor_enabled,
        u.two_factor_required, u.last_login_at
    FROM public.staff_users u
    WHERE app_security.has_permission('users.view') AND app_security.has_global_scope()
    ORDER BY u.email
$function$;
GRANT EXECUTE ON FUNCTION app_security.staff_directory() TO app_runtime;

-- Role count is metadata; the system-role definition and permissions stay locked.
CREATE OR REPLACE FUNCTION app_security.roles_catalog() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('roles.manage')
            AND app_security.has_global_scope() THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', r.id, 'name', r.name, 'description', r.description,
            'is_system', r.is_system,
            'users_count', (SELECT count(*) FROM public.user_roles ur WHERE ur.role_id = r.id),
            'permissions', COALESCE((SELECT jsonb_agg(rp.permission_code ORDER BY rp.permission_code)
                FROM public.role_permissions rp WHERE rp.role_id = r.id), '[]'::jsonb)
        ) ORDER BY r.name) FROM public.roles r), '[]'::jsonb)
        ELSE NULL END
$function$;
GRANT EXECUTE ON FUNCTION app_security.roles_catalog() TO app_runtime;

-- Column-level staff metadata only; the complaint source is always separately
-- constrained by AccessScope/RLS before these names are surfaced.
GRANT SELECT (id, full_name) ON public.staff_users TO app_runtime;

-- The current complaint set, rather than the full staff directory, determines
-- which agent names are returned to a scoped complaints viewer.
CREATE FUNCTION app_security.visible_complaint_agents()
RETURNS TABLE (id uuid, full_name text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT DISTINCT u.id, u.full_name
    FROM public.complaints c
    JOIN public.staff_users u ON u.id = c.assigned_agent_id
    WHERE c.assigned_agent_id IS NOT NULL
      AND app_security.has_permission('complaints.view')
      AND app_security.staff_scope_matches(
          c.company_id, c.insurance_type, c.assigned_agent_id, c.team_id)
    ORDER BY u.full_name, u.id
    LIMIT 500
$function$;
GRANT EXECUTE ON FUNCTION app_security.visible_complaint_agents() TO app_runtime;

-- Access editor option list. The caller must hold global access-management
-- authority; no company/team/role catalog is returned to an ordinary agent.
CREATE FUNCTION app_security.access_admin_options() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('users.manage_access')
            AND app_security.has_global_scope() THEN jsonb_build_object(
        'companies', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'name_en', c.name_en, 'name_ar', c.name_ar) ORDER BY c.name_en)
            FROM public.insurance_companies c WHERE c.is_active), '[]'::jsonb),
        'teams', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', t.id, 'name', t.name,
            'member_ids', COALESCE((SELECT jsonb_agg(tm.user_id::text ORDER BY tm.user_id)
                FROM public.team_members tm WHERE tm.team_id = t.id), '[]'::jsonb))
            ORDER BY t.name) FROM public.teams t), '[]'::jsonb),
        'roles', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', r.id, 'name', r.name, 'description', r.description,
            'is_system', r.is_system,
            'users_count', (SELECT count(*) FROM public.user_roles ur WHERE ur.role_id = r.id),
            'permissions', COALESCE((SELECT jsonb_agg(rp.permission_code ORDER BY rp.permission_code)
                FROM public.role_permissions rp WHERE rp.role_id = r.id), '[]'::jsonb))
            ORDER BY r.name) FROM public.roles r), '[]'::jsonb)
    ) ELSE NULL END
$function$;
GRANT EXECUTE ON FUNCTION app_security.access_admin_options() TO app_runtime;

-- Team administration invalidates every affected member session so team-scope
-- changes take effect immediately, including in open browsers.
CREATE FUNCTION app_security.admin_teams() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('teams.manage')
            AND app_security.has_global_scope() THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', t.id, 'name', t.name, 'lead_staff_id', t.lead_staff_id,
            'member_ids', COALESCE((SELECT jsonb_agg(tm.user_id::text ORDER BY tm.user_id)
                FROM public.team_members tm WHERE tm.team_id = t.id), '[]'::jsonb))
            ORDER BY t.name) FROM public.teams t), '[]'::jsonb)
        ELSE NULL END
$function$;

CREATE FUNCTION app_security.save_team(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_name text; v_lead uuid; v_before uuid[]; v_after uuid[];
    v_user uuid; v_members text[];
BEGIN
    v_name := btrim(p_data->>'name');
    v_lead := NULLIF(p_data->>'lead_staff_id', '')::uuid;
    IF NOT app_security.has_permission('teams.manage')
       OR NOT app_security.has_global_scope()
       OR length(v_name) NOT BETWEEN 2 AND 80
       OR jsonb_typeof(p_data->'member_ids') <> 'array'
       OR jsonb_array_length(p_data->'member_ids') > 100 THEN
        RAISE EXCEPTION 'team change forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    SELECT array_agg(value ORDER BY value) INTO v_members
        FROM (SELECT DISTINCT jsonb_array_elements_text(p_data->'member_ids') AS value) x;
    IF cardinality(COALESCE(v_members, ARRAY[]::text[])) <>
           jsonb_array_length(p_data->'member_ids')
       OR EXISTS (SELECT 1 FROM unnest(COALESCE(v_members, ARRAY[]::text[])) AS member_id
            WHERE NOT EXISTS (SELECT 1 FROM public.staff_users u
                WHERE u.id = member_id::uuid AND u.status = 'active'))
       OR (v_lead IS NOT NULL AND NOT (v_lead::text = ANY(COALESCE(v_members, ARRAY[]::text[])))) THEN
        RAISE EXCEPTION 'team members must be distinct active staff and include the lead'
            USING ERRCODE = '22023';
    END IF;
    IF p_id IS NULL THEN
        INSERT INTO public.teams(name, lead_staff_id) VALUES (v_name, v_lead)
            RETURNING id INTO v_id;
        v_before := ARRAY[]::uuid[];
    ELSE
        SELECT t.id INTO v_id FROM public.teams t WHERE t.id = p_id FOR UPDATE;
        IF v_id IS NULL THEN RETURN NULL; END IF;
        SELECT COALESCE(array_agg(tm.user_id ORDER BY tm.user_id), ARRAY[]::uuid[])
            INTO v_before FROM public.team_members tm WHERE tm.team_id = p_id;
        UPDATE public.teams SET name = v_name, lead_staff_id = v_lead WHERE id = p_id;
    END IF;
    DELETE FROM public.team_members WHERE team_id = v_id;
    INSERT INTO public.team_members(team_id, user_id)
        SELECT v_id, member_id::uuid
        FROM unnest(COALESCE(v_members, ARRAY[]::text[])) AS member_list(member_id);
    SELECT COALESCE(array_agg(tm.user_id ORDER BY tm.user_id), ARRAY[]::uuid[])
        INTO v_after FROM public.team_members tm WHERE tm.team_id = v_id;
    IF v_before IS DISTINCT FROM v_after THEN
        FOR v_user IN SELECT DISTINCT member_id FROM unnest(v_before || v_after) AS changed(member_id)
        LOOP
            UPDATE public.staff_users SET token_version = token_version + 1,
                updated_at = now() WHERE id = v_user;
            UPDATE public.auth_sessions SET revoked_at = now()
                WHERE subject_kind = 'staff' AND subject_id = v_user AND revoked_at IS NULL;
            INSERT INTO public.access_change_log(actor_staff_id, target_user_id,
                change_type, before_state, after_state)
                VALUES (app_security.actor_id(), v_user, 'team_membership_changed',
                    jsonb_build_object('team_id', v_id, 'member', v_user = ANY(v_before)),
                    jsonb_build_object('team_id', v_id, 'member', v_user = ANY(v_after)));
        END LOOP;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        after_state) VALUES (app_security.actor_id(), 'team_saved', 'team', v_id,
            jsonb_build_object('name', v_name, 'member_count', cardinality(v_after)));
    RETURN v_id;
END;
$function$;

-- Safe reference lists for configuring auto-assignment targets; no credentials
-- or out-of-scope ticket/customer data are returned.
CREATE FUNCTION app_security.admin_assignment_options() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('settings.manage')
            AND app_security.has_global_scope() THEN jsonb_build_object(
        'companies', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'name_en', c.name_en, 'name_ar', c.name_ar)
            ORDER BY c.name_en) FROM public.insurance_companies c WHERE c.is_active), '[]'::jsonb),
        'categories', COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'company_id', c.company_id, 'name_en', c.name_en)
            ORDER BY c.name_en) FROM public.categories c WHERE c.is_active), '[]'::jsonb),
        'teams', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', t.id, 'name', t.name)
            ORDER BY t.name) FROM public.teams t), '[]'::jsonb),
        'agents', COALESCE((SELECT jsonb_agg(jsonb_build_object('id', u.id, 'full_name', u.full_name)
            ORDER BY u.full_name) FROM public.staff_users u WHERE u.status = 'active'), '[]'::jsonb)
    ) ELSE NULL END
$function$;

-- Preserve the Phase 3 escalation function and its existing permission contract;
-- delivery remains the responsibility of the restricted worker.

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.admin_teams(),
    app_security.save_team(uuid, jsonb),
    app_security.access_admin_options(),
    app_security.admin_assignment_options(),
    app_security.visible_complaint_agents()
    TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.staff_escalate(uuid, timestamptz, text)
    TO app_runtime;
