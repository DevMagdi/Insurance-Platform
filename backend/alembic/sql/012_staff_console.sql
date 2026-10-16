-- Phase 7 staff-console support. New reads and writes remain narrow,
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

-- Auto-assignment is opt-in and requires an explicit active target. The worker
-- still rechecks that an agent belongs to the target team and company scope.
CREATE FUNCTION app_security.admin_assignment_rules() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('settings.manage')
            AND app_security.has_global_scope() THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', r.id, 'company_id', r.company_id, 'insurance_type', r.insurance_type,
            'category_id', r.category_id, 'priority', r.priority,
            'target_team_id', r.target_team_id, 'target_agent_id', r.target_agent_id,
            'strategy', r.strategy, 'sort_order', r.sort_order, 'is_active', r.is_active)
            ORDER BY r.sort_order, r.created_at, r.id) FROM public.assignment_rules r), '[]'::jsonb)
        ELSE NULL END
$function$;

CREATE FUNCTION app_security.save_assignment_rule(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_type insurance_type; v_category uuid;
    v_priority complaint_priority; v_team uuid; v_agent uuid; v_strategy text;
    v_order integer; v_active boolean; v_before jsonb;
BEGIN
    v_company := NULLIF(p_data->>'company_id', '')::uuid;
    v_type := NULLIF(p_data->>'insurance_type', '')::insurance_type;
    v_category := NULLIF(p_data->>'category_id', '')::uuid;
    v_priority := NULLIF(p_data->>'priority', '')::complaint_priority;
    v_team := NULLIF(p_data->>'target_team_id', '')::uuid;
    v_agent := NULLIF(p_data->>'target_agent_id', '')::uuid;
    v_strategy := p_data->>'strategy';
    v_order := (p_data->>'sort_order')::integer;
    v_active := (p_data->>'is_active')::boolean;
    IF NOT app_security.has_permission('settings.manage')
       OR NOT app_security.has_global_scope()
       OR v_strategy NOT IN ('round_robin','least_workload','by_company')
       OR v_order NOT BETWEEN 0 AND 100000
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR (v_team IS NULL AND v_agent IS NULL)
       OR (v_company IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM public.insurance_companies c WHERE c.id = v_company AND c.is_active))
       OR (v_category IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM public.categories c WHERE c.id = v_category
              AND c.company_id IS NOT DISTINCT FROM v_company AND c.is_active))
       OR (v_team IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.teams t WHERE t.id = v_team))
       OR (v_agent IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM public.staff_users u WHERE u.id = v_agent AND u.status = 'active')) THEN
        RAISE EXCEPTION 'assignment rule forbidden or invalid' USING ERRCODE = '22023';
    END IF;
    IF p_id IS NULL THEN
        INSERT INTO public.assignment_rules(company_id, insurance_type, category_id,
            priority, target_team_id, target_agent_id, strategy, sort_order, is_active)
        VALUES (v_company, v_type, v_category, v_priority, v_team, v_agent,
            v_strategy, v_order, v_active) RETURNING id INTO v_id;
    ELSE
        SELECT jsonb_build_object('is_active', r.is_active, 'sort_order', r.sort_order)
            INTO v_before FROM public.assignment_rules r WHERE r.id = p_id FOR UPDATE;
        IF v_before IS NULL THEN RETURN NULL; END IF;
        UPDATE public.assignment_rules SET company_id = v_company,
            insurance_type = v_type, category_id = v_category, priority = v_priority,
            target_team_id = v_team, target_agent_id = v_agent, strategy = v_strategy,
            sort_order = v_order, is_active = v_active WHERE id = p_id;
        v_id := p_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        before_state, after_state) VALUES (app_security.actor_id(), 'assignment_rule_saved',
        'assignment_rule', v_id, COALESCE(v_before, '{}'::jsonb),
        jsonb_build_object('is_active', v_active, 'strategy', v_strategy));
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

-- Scheduled-report definitions are stored per owner. There is deliberately no
-- active scheduler in this release; the API reports delivery_ready=false.
CREATE FUNCTION app_security.save_report_schedule(
    p_owner uuid, p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_name text; v_cron text; v_zone text; v_active boolean;
    v_recipients text[]; v_filters jsonb;
BEGIN
    v_name := btrim(p_data->>'name');
    v_cron := btrim(p_data->>'cron_expression');
    v_zone := p_data->>'time_zone';
    v_active := COALESCE((p_data->>'is_active')::boolean, false);
    v_filters := p_data->'filters';
    IF NOT app_security.has_permission('reports.manage')
       OR p_owner <> app_security.actor_id()
       OR length(v_name) NOT BETWEEN 2 AND 120
       OR v_cron !~ '^[0-9*/,-]+ [0-9*/,-]+ [0-9*/,-]+ [0-9*/,-]+ [0-9*/,-]+$'
       OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names z WHERE z.name = v_zone)
       OR jsonb_typeof(v_filters) <> 'object'
       OR jsonb_typeof(p_data->'recipient_emails') <> 'array'
       OR jsonb_array_length(p_data->'recipient_emails') NOT BETWEEN 1 AND 10
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_data->'recipient_emails') AS addr
            WHERE addr <> lower(addr) OR addr !~ '^[^@ ]+@[^@ ]+\.[^@ ]+$'
               OR length(addr) > 254) THEN
        RAISE EXCEPTION 'scheduled report forbidden or invalid' USING ERRCODE = '22023';
    END IF;
    SELECT array_agg(addr ORDER BY addr) INTO v_recipients
        FROM (SELECT DISTINCT jsonb_array_elements_text(p_data->'recipient_emails') AS addr) x;
    IF cardinality(v_recipients) <> jsonb_array_length(p_data->'recipient_emails') THEN
        RAISE EXCEPTION 'duplicate scheduled report recipients' USING ERRCODE = '22023';
    END IF;
    IF p_id IS NULL THEN
        INSERT INTO public.report_schedules(owner_staff_id, name, filters, cron_expression,
            time_zone, recipient_emails, is_active)
        VALUES (p_owner, v_name, v_filters, v_cron, v_zone, v_recipients, v_active)
        RETURNING id INTO v_id;
    ELSE
        UPDATE public.report_schedules SET name = v_name, filters = v_filters,
            cron_expression = v_cron, time_zone = v_zone,
            recipient_emails = v_recipients, is_active = v_active
        WHERE id = p_id AND owner_staff_id = p_owner RETURNING id INTO v_id;
        IF v_id IS NULL THEN RETURN NULL; END IF;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        after_state) VALUES (app_security.actor_id(), 'report_schedule_saved',
        'report_schedule', v_id, jsonb_build_object('is_active', v_active));
    RETURN v_id;
END;
$function$;

-- Integration configuration stores only secret-manager references, never a
-- password/token. Runtime adapters remain independently fail-closed.
CREATE FUNCTION app_security.admin_integrations() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('integrations.manage')
            AND app_security.has_global_scope() THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'provider', i.provider, 'company_id', i.company_id,
            'enabled', i.is_enabled,
            'secret_configured', (i.secret_ref IS NOT NULL OR i.config_ciphertext IS NOT NULL),
            'updated_at', i.updated_at,
            'runtime_ready', false) ORDER BY i.provider, i.company_id)
            FROM public.integrations_config i), '[]'::jsonb)
        ELSE NULL END
$function$;

CREATE FUNCTION app_security.save_integration(
    p_provider text, p_company uuid, p_data jsonb) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_enabled boolean; v_secret_ref text;
BEGIN
    v_enabled := (p_data->>'enabled')::boolean;
    v_secret_ref := NULLIF(p_data->>'secret_ref', '');
    IF v_enabled AND v_secret_ref IS NULL THEN
        SELECT i.secret_ref INTO v_secret_ref FROM public.integrations_config i
        WHERE i.provider = p_provider AND i.company_id IS NOT DISTINCT FROM p_company;
    END IF;
    IF NOT app_security.has_permission('integrations.manage')
       OR NOT app_security.has_global_scope()
       OR p_provider NOT IN ('email','whatsapp')
       OR jsonb_typeof(p_data->'enabled') <> 'boolean'
       OR (v_enabled AND (v_secret_ref IS NULL
            OR v_secret_ref !~ '^vault://[A-Za-z0-9/_-]{1,150}$'))
       OR (p_company IS NOT NULL AND NOT EXISTS
            (SELECT 1 FROM public.insurance_companies c WHERE c.id = p_company AND c.is_active)) THEN
        RAISE EXCEPTION 'integration setting forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.integrations_config(company_id, provider, secret_ref, is_enabled, updated_at)
        VALUES (p_company, p_provider, v_secret_ref, v_enabled, now())
        ON CONFLICT (company_id, provider) DO UPDATE SET secret_ref = EXCLUDED.secret_ref,
            is_enabled = EXCLUDED.is_enabled, updated_at = now();
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, after_state)
        VALUES (app_security.actor_id(), CASE WHEN v_enabled THEN 'integration_configured'
            ELSE 'integration_disconnected' END, 'integration',
            jsonb_build_object('provider', p_provider, 'company_id', p_company,
                'enabled', v_enabled, 'secret_configured', v_secret_ref IS NOT NULL));
    RETURN true;
END;
$function$;

-- Escalation emails require both transfer authority and explicit email-send
-- authority. A queued notice is not a claim of successful delivery.
CREATE OR REPLACE FUNCTION app_security.staff_escalate(
    p_id uuid, p_created timestamptz, p_reason text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_email text;
BEGIN
    SELECT * INTO v_ticket FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR NOT app_security.has_permission('complaints.escalate')
       OR NOT app_security.has_permission('integrations.send_email')
       OR NOT app_security.can_read_complaint(v_ticket.company_id,
            v_ticket.insurance_type, v_ticket.assigned_agent_id,
            v_ticket.team_id, v_ticket.customer_id)
       OR v_ticket.merged_into_id IS NOT NULL
       OR v_ticket.status NOT IN ('new','assigned','in_review','reopened')
       OR length(btrim(p_reason)) NOT BETWEEN 10 AND 500 THEN
        RETURN false;
    END IF;
    SELECT email INTO v_email FROM public.insurance_companies co,
        LATERAL unnest(co.contact_emails) AS email
        WHERE co.id = v_ticket.company_id
          AND email ~ '^[^@ ]+@[^@ ]+\.[^@ ]+$'
        LIMIT 1;
    IF v_email IS NULL THEN RETURN false; END IF;
    UPDATE public.complaints SET status = 'transferred', updated_at = now()
        WHERE id = p_id AND created_at = p_created;
    INSERT INTO public.insurer_outbox
        (complaint_id, complaint_created_at, recipient_email, ticket_no, reason)
        VALUES (p_id, p_created, v_email, v_ticket.ticket_no, btrim(p_reason));
    INSERT INTO public.complaint_events
        (complaint_id, complaint_created_at, event_type, from_status,
         to_status, actor_staff_id, metadata)
        VALUES (p_id, p_created, 'escalated', v_ticket.status,
            'transferred', app_security.actor_id(),
            jsonb_build_object('reason', btrim(p_reason)));
    INSERT INTO public.audit_logs
        (actor_staff_id, action, resource_type, resource_id,
         complaint_id, complaint_created_at, after_state)
        VALUES (app_security.actor_id(), 'escalated', 'complaint', p_id,
            p_id, p_created, jsonb_build_object('insurer', v_ticket.company_id));
    INSERT INTO public.notification_outbox
        (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
        VALUES (p_id, p_created, v_ticket.customer_id, 'status_changed',
            jsonb_build_object('ticket_no', v_ticket.ticket_no, 'status', 'transferred'));
    INSERT INTO public.notifications
        (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
        VALUES (v_ticket.customer_id, p_id, p_created, 'status_changed',
            jsonb_build_object('ticket_no', v_ticket.ticket_no, 'status', 'transferred'));
    RETURN true;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.admin_teams(),
    app_security.save_team(uuid, jsonb),
    app_security.admin_assignment_rules(),
    app_security.save_assignment_rule(uuid, jsonb),
    app_security.admin_integrations(),
    app_security.save_integration(text, uuid, jsonb),
    app_security.access_admin_options(),
    app_security.admin_assignment_options(),
    app_security.save_report_schedule(uuid, uuid, jsonb),
    app_security.visible_complaint_agents()
    TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.staff_escalate(uuid, timestamptz, text)
    TO app_runtime;
