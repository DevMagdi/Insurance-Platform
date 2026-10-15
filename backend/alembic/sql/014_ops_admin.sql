-- Operational administration: assignment rules, integration settings and
-- duplicate detection. Every write is a guarded definer command with audit.
ALTER TABLE assignment_rules ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX ix_assignment_rules_team ON assignment_rules (target_team_id, sort_order)
    WHERE is_active;

-- Auto-assignment now distinguishes the three documented strategies. Candidates
-- are always filtered by agent_can_see(), so a rule can never route a ticket to
-- an agent whose own company/type/ownership scope does not cover it.
CREATE OR REPLACE FUNCTION app_security.auto_assign_tickets(p_limit integer DEFAULT 20)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_rule assignment_rules%ROWTYPE;
    v_agent uuid; v_team uuid; v_done integer := 0;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN 0; END IF;
    FOR v_ticket IN
        SELECT c.* FROM public.complaints c
        WHERE c.status = 'new' AND c.assigned_agent_id IS NULL
          AND c.merged_into_id IS NULL
          AND EXISTS (SELECT 1 FROM public.assignment_rules ar
              WHERE ar.is_active
                AND (ar.company_id IS NULL OR ar.company_id = c.company_id)
                AND (ar.insurance_type IS NULL OR ar.insurance_type = c.insurance_type)
                AND (ar.priority IS NULL OR ar.priority = c.priority)
                AND (ar.category_id IS NULL OR ar.category_id = c.category_id))
        ORDER BY c.created_at, c.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        SELECT * INTO v_rule FROM public.assignment_rules ar
        WHERE ar.is_active
          AND (ar.company_id IS NULL OR ar.company_id = v_ticket.company_id)
          AND (ar.insurance_type IS NULL OR ar.insurance_type = v_ticket.insurance_type)
          AND (ar.priority IS NULL OR ar.priority = v_ticket.priority)
          AND (ar.category_id IS NULL OR ar.category_id = v_ticket.category_id)
        ORDER BY ar.sort_order, (ar.company_id IS NOT NULL)::integer DESC,
                 (ar.insurance_type IS NOT NULL)::integer DESC,
                 (ar.priority IS NOT NULL)::integer DESC, ar.id
        LIMIT 1 FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN CONTINUE; END IF;
        v_agent := NULL;
        v_team := v_rule.target_team_id;
        IF v_rule.target_agent_id IS NOT NULL AND
           app_security.agent_can_see(v_rule.target_agent_id, v_ticket.company_id,
              v_ticket.insurance_type, v_team) AND
           (v_team IS NULL OR EXISTS (SELECT 1 FROM public.team_members tm
               WHERE tm.user_id = v_rule.target_agent_id AND tm.team_id = v_team)) THEN
            v_agent := v_rule.target_agent_id;
        ELSIF v_team IS NOT NULL THEN
            IF v_rule.strategy = 'round_robin' THEN
                SELECT candidates.user_id INTO v_agent FROM public.team_members candidates
                WHERE candidates.team_id = v_team
                  AND candidates.user_id > COALESCE(v_rule.last_assigned_agent_id,
                      '00000000-0000-0000-0000-000000000000'::uuid)
                  AND app_security.agent_can_see(candidates.user_id, v_ticket.company_id,
                      v_ticket.insurance_type, v_team)
                ORDER BY candidates.user_id LIMIT 1;
                IF v_agent IS NULL THEN
                    SELECT candidates.user_id INTO v_agent FROM public.team_members candidates
                    WHERE candidates.team_id = v_team
                      AND app_security.agent_can_see(candidates.user_id, v_ticket.company_id,
                          v_ticket.insurance_type, v_team)
                    ORDER BY candidates.user_id LIMIT 1;
                END IF;
            ELSIF v_rule.strategy = 'by_company' THEN
                -- Prefer the agent with the deepest history for this insurer,
                -- falling back to the lightest open workload.
                SELECT candidates.user_id INTO v_agent FROM public.team_members candidates
                WHERE candidates.team_id = v_team
                  AND app_security.agent_can_see(candidates.user_id, v_ticket.company_id,
                      v_ticket.insurance_type, v_team)
                ORDER BY (SELECT count(*) FROM public.complaints history
                        WHERE history.assigned_agent_id = candidates.user_id
                          AND history.company_id = v_ticket.company_id
                          AND history.status = 'resolved') DESC,
                    (SELECT count(*) FROM public.complaints open_tickets
                        WHERE open_tickets.assigned_agent_id = candidates.user_id
                          AND open_tickets.status <> 'resolved'),
                    candidates.user_id LIMIT 1;
            ELSE
                SELECT candidates.user_id INTO v_agent FROM public.team_members candidates
                WHERE candidates.team_id = v_team
                  AND app_security.agent_can_see(candidates.user_id, v_ticket.company_id,
                      v_ticket.insurance_type, v_team)
                ORDER BY (SELECT count(*) FROM public.complaints open_tickets
                    WHERE open_tickets.assigned_agent_id = candidates.user_id
                      AND open_tickets.status <> 'resolved'), candidates.user_id LIMIT 1;
            END IF;
        END IF;
        IF v_agent IS NULL THEN CONTINUE; END IF;
        UPDATE public.assignment_rules SET last_assigned_agent_id = v_agent,
            updated_at = now() WHERE id = v_rule.id;
        UPDATE public.complaints SET assigned_agent_id = v_agent, team_id = v_team,
            status = 'assigned', updated_at = now()
            WHERE id = v_ticket.id AND created_at = v_ticket.created_at;
        INSERT INTO public.complaint_events
            (complaint_id, complaint_created_at, event_type, from_status,
             to_status, metadata)
            VALUES (v_ticket.id, v_ticket.created_at, 'auto_assigned', 'new',
                'assigned', jsonb_build_object('rule_id', v_rule.id,
                    'strategy', v_rule.strategy));
        INSERT INTO public.audit_logs
            (action, resource_type, resource_id, complaint_id,
             complaint_created_at, after_state)
            VALUES ('auto_assigned', 'complaint', v_ticket.id, v_ticket.id,
                v_ticket.created_at, jsonb_build_object('agent_id', v_agent,
                    'strategy', v_rule.strategy));
        INSERT INTO public.notifications
            (recipient_staff_id, complaint_id, complaint_created_at, kind, payload)
            VALUES (v_agent, v_ticket.id, v_ticket.created_at, 'assigned',
                jsonb_build_object('ticket_no', v_ticket.ticket_no));
        INSERT INTO public.notifications
            (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
            VALUES (v_ticket.customer_id, v_ticket.id, v_ticket.created_at,
                'status_changed', jsonb_build_object('ticket_no', v_ticket.ticket_no,
                    'status', 'assigned'));
        INSERT INTO public.notification_outbox
            (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
            VALUES (v_ticket.id, v_ticket.created_at, v_ticket.customer_id,
                'status_changed', jsonb_build_object('ticket_no', v_ticket.ticket_no,
                    'status', 'assigned'));
        v_done := v_done + 1;
    END LOOP;
    RETURN v_done;
END;
$function$;

CREATE FUNCTION app_security.save_assignment_rule(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_type insurance_type; v_team uuid; v_agent uuid;
    v_prior_company uuid; v_prior_type insurance_type;
BEGIN
    v_company := NULLIF(p_data->>'company_id','')::uuid;
    v_team := NULLIF(p_data->>'target_team_id','')::uuid;
    v_agent := NULLIF(p_data->>'target_agent_id','')::uuid;
    IF p_data->>'insurance_type' NOT IN ('health','general','both')
       AND p_data->>'insurance_type' IS NOT NULL THEN
        RAISE EXCEPTION 'unknown insurance type' USING ERRCODE = '22023';
    END IF;
    v_type := (p_data->>'insurance_type')::insurance_type;
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('complaints.assign')
       OR NOT app_security.can_manage_catalog(v_company, v_type)
       OR jsonb_typeof(p_data) <> 'object'
       OR p_data - ARRAY['company_id','insurance_type','category_id','priority',
            'target_team_id','target_agent_id','strategy','sort_order','is_active']
            <> '{}'::jsonb
       OR COALESCE(p_data->>'strategy','least_workload')
            NOT IN ('round_robin','least_workload','by_company')
       OR ((p_data->>'priority') NOT IN ('low','normal','high','urgent')
           AND p_data->>'priority' IS NOT NULL)
       OR (p_data->>'sort_order')::integer NOT BETWEEN 0 AND 10000
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR (v_team IS NULL AND v_agent IS NULL)
       OR (v_team IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.teams t WHERE t.id = v_team))
       OR (v_agent IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.staff_users u WHERE u.id = v_agent
               AND u.status = 'active'))
       OR (v_company IS NOT NULL AND v_agent IS NOT NULL
           AND NOT app_security.agent_can_see(v_agent, v_company,
               COALESCE(v_type, 'both'), v_team))
       OR (v_company IS NOT NULL AND v_team IS NOT NULL AND v_agent IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM public.team_members tm
               WHERE tm.team_id = v_team AND tm.user_id = v_agent))
       OR (NULLIF(p_data->>'category_id','') IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.categories c
            WHERE c.id = (p_data->>'category_id')::uuid
              AND (c.company_id IS NULL OR c.company_id = v_company)))
       OR (v_company IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.insurance_companies co WHERE co.id = v_company)) THEN
        RAISE EXCEPTION 'assignment rule forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NOT NULL THEN
        SELECT company_id, insurance_type INTO v_prior_company, v_prior_type
            FROM public.assignment_rules WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_prior_company IS DISTINCT FROM v_company
           OR NOT app_security.can_manage_catalog(v_prior_company, v_prior_type) THEN
            RAISE EXCEPTION 'assignment rule cannot move across scope'
                USING ERRCODE = '42501';
        END IF;
        UPDATE public.assignment_rules SET company_id = v_company,
            insurance_type = v_type, category_id = NULLIF(p_data->>'category_id','')::uuid,
            priority = (p_data->>'priority')::complaint_priority,
            target_team_id = v_team, target_agent_id = v_agent,
            strategy = COALESCE(p_data->>'strategy','least_workload'),
            sort_order = (p_data->>'sort_order')::integer,
            is_active = (p_data->>'is_active')::boolean, updated_at = now()
            WHERE id = p_id;
        v_id := p_id;
    ELSE
        INSERT INTO public.assignment_rules(company_id,insurance_type,category_id,
            priority,target_team_id,target_agent_id,strategy,sort_order,is_active)
        VALUES (v_company,v_type,NULLIF(p_data->>'category_id','')::uuid,
            (p_data->>'priority')::complaint_priority,v_team,v_agent,
            COALESCE(p_data->>'strategy','least_workload'),
            (p_data->>'sort_order')::integer,(p_data->>'is_active')::boolean)
        RETURNING id INTO v_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'assignment_rule_saved','assignment_rule',v_id,
            jsonb_build_object('company_id',v_company,'insurance_type',v_type,
                'strategy',p_data->>'strategy','target_team_id',v_team,
                'target_agent_id',v_agent,'active',(p_data->>'is_active')::boolean));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.delete_assignment_rule(p_id uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_company uuid; v_type insurance_type;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('complaints.assign') THEN
        RAISE EXCEPTION 'assignment rule forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT company_id, insurance_type INTO v_company, v_type
        FROM public.assignment_rules WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF NOT app_security.can_manage_catalog(v_company, v_type) THEN
        RAISE EXCEPTION 'assignment rule outside scope' USING ERRCODE = '42501';
    END IF;
    DELETE FROM public.assignment_rules WHERE id = p_id;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id)
        VALUES (app_security.actor_id(),'assignment_rule_deleted','assignment_rule',p_id);
    RETURN true;
END;
$function$;

-- Dry-run: which agents would be eligible right now, and in what order.
CREATE FUNCTION app_security.preview_assignment(
    p_company uuid, p_type insurance_type, p_priority complaint_priority,
    p_category uuid, p_team uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_rule assignment_rules%ROWTYPE; v_team uuid; v_candidates jsonb;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('complaints.assign')
       OR NOT app_security.can_manage_catalog(p_company, p_type) THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_rule FROM public.assignment_rules ar
    WHERE ar.is_active
      AND (ar.company_id IS NULL OR ar.company_id = p_company)
      AND (ar.insurance_type IS NULL OR ar.insurance_type = p_type)
      AND (ar.priority IS NULL OR ar.priority = p_priority)
      AND (ar.category_id IS NULL OR ar.category_id = p_category)
    ORDER BY ar.sort_order, (ar.company_id IS NOT NULL)::integer DESC,
             (ar.insurance_type IS NOT NULL)::integer DESC,
             (ar.priority IS NOT NULL)::integer DESC, ar.id
    LIMIT 1;
    IF v_rule.id IS NULL THEN RETURN jsonb_build_object('matched', false); END IF;
    v_team := COALESCE(p_team, v_rule.target_team_id);
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
            'agent_id', ranked.id,
            'full_name', ranked.full_name,
            'open_tickets', ranked.open_tickets,
            'company_resolved', ranked.company_resolved,
            'team_id', ranked.team_id) ORDER BY ranked.rank), '[]'::jsonb)
        INTO v_candidates
    FROM (
        SELECT candidate.id, candidate.full_name,
            (SELECT count(*) FROM public.complaints open_tickets
                WHERE open_tickets.assigned_agent_id = candidate.id
                  AND open_tickets.status <> 'resolved') AS open_tickets,
            (SELECT count(*) FROM public.complaints history
                WHERE history.assigned_agent_id = candidate.id
                  AND history.company_id = p_company
                  AND history.status = 'resolved') AS company_resolved,
            (SELECT tm.team_id FROM public.team_members tm
                WHERE tm.user_id = candidate.id LIMIT 1) AS team_id,
            row_number() OVER (ORDER BY
                CASE WHEN v_rule.strategy = 'by_company' THEN
                    (SELECT count(*) FROM public.complaints history
                        WHERE history.assigned_agent_id = candidate.id
                          AND history.company_id = p_company
                          AND history.status = 'resolved') END DESC NULLS LAST,
                (SELECT count(*) FROM public.complaints open_tickets
                    WHERE open_tickets.assigned_agent_id = candidate.id
                      AND open_tickets.status <> 'resolved') ASC,
                candidate.id) AS rank
        FROM public.staff_users candidate
        WHERE candidate.status = 'active'
          AND (v_rule.target_agent_id IS NULL OR candidate.id = v_rule.target_agent_id)
          AND (v_team IS NULL OR EXISTS (SELECT 1 FROM public.team_members tm
              WHERE tm.team_id = v_team AND tm.user_id = candidate.id))
          AND app_security.agent_can_see(candidate.id, p_company,
              COALESCE(p_type, 'both'), v_team)
    ) ranked;
    RETURN jsonb_build_object('matched', true, 'rule_id', v_rule.id,
        'strategy', v_rule.strategy, 'team_id', v_team,
        'auto_assignment', (SELECT count(*) > 0 FROM public.assignment_rules
            WHERE is_active),
        'candidates', v_candidates);
END;
$function$;

-- Provider settings. Cleartext or inline credentials are never accepted: the
-- secret itself lives in a vault and is referenced by secret_ref.
CREATE FUNCTION app_security.save_integration(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_prior_company uuid; v_key text;
    v_enabled boolean;
BEGIN
    v_company := NULLIF(p_data->>'company_id','')::uuid;
    v_enabled := COALESCE((p_data->>'is_enabled')::boolean, false);
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('integrations.manage')
       OR NOT app_security.can_manage_catalog(v_company)
       OR jsonb_typeof(p_data) <> 'object'
       OR p_data - ARRAY['company_id','provider','is_enabled','secret_ref','config']
            <> '{}'::jsonb
       OR p_data->>'provider' NOT IN ('email','whatsapp','sms','ai_voice','virus_scan')
       OR jsonb_typeof(p_data->'is_enabled') <> 'boolean'
       OR COALESCE(jsonb_typeof(p_data->'config'),'missing') <> 'object'
       OR COALESCE((SELECT count(*) FROM jsonb_object_keys(p_data->'config')),0) > 20
       OR (p_data->>'secret_ref' IS NOT NULL
           AND p_data->>'secret_ref' !~ '^[A-Za-z0-9_.:/-]{3,200}$')
       OR (v_enabled AND COALESCE(p_data->>'secret_ref','') = '') THEN
        RAISE EXCEPTION 'integration settings forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    FOR v_key IN SELECT jsonb_object_keys(p_data->'config') LOOP
        IF v_key !~ '^[a-z][a-z0-9_]{1,40}$'
           OR v_key ~ '(secret|token|password|api_key|private|credential|key)'
           OR jsonb_typeof(p_data->'config'->v_key) NOT IN ('string','number','boolean')
           OR length(p_data->'config'->>v_key) > 500 THEN
            RAISE EXCEPTION 'integration config must reference secrets by name'
                USING ERRCODE = '42501';
        END IF;
    END LOOP;
    IF p_id IS NULL THEN
        INSERT INTO public.integrations_config(company_id,provider,secret_ref,
            is_enabled,updated_at)
        VALUES (v_company, p_data->>'provider',
            NULLIF(p_data->>'secret_ref',''), v_enabled, now())
        ON CONFLICT (company_id, provider) DO UPDATE SET
            secret_ref = EXCLUDED.secret_ref, is_enabled = EXCLUDED.is_enabled,
            updated_at = now()
        RETURNING id INTO v_id;
    ELSE
        SELECT company_id INTO v_prior_company FROM public.integrations_config
            WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_prior_company IS DISTINCT FROM v_company
           OR NOT app_security.can_manage_catalog(v_prior_company) THEN
            RAISE EXCEPTION 'integration cannot move across scope'
                USING ERRCODE = '42501';
        END IF;
        UPDATE public.integrations_config SET provider = p_data->>'provider',
            secret_ref = NULLIF(p_data->>'secret_ref',''), is_enabled = v_enabled,
            updated_at = now() WHERE id = p_id;
        v_id := p_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'integration_saved','integration',v_id,
            jsonb_build_object('provider',p_data->>'provider','company_id',v_company,
                'enabled',v_enabled,'secret_reference',p_data->>'secret_ref' IS NOT NULL));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.admin_integrations() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.is_active_staff()
        AND app_security.has_permission('integrations.manage') THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id', i.id, 'company_id', i.company_id, 'provider', i.provider,
            'is_enabled', i.is_enabled, 'secret_reference', i.secret_ref IS NOT NULL,
            'updated_at', i.updated_at) ORDER BY i.provider, i.id)
          FROM public.integrations_config i
          WHERE (i.company_id IS NULL AND app_security.has_global_scope())
             OR (i.company_id IS NOT NULL AND app_security.has_company_scope(i.company_id))),
        '[]'::jsonb)
        ELSE NULL END
$function$;

CREATE FUNCTION app_security.delete_integration(p_id uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_company uuid;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('integrations.manage') THEN
        RAISE EXCEPTION 'integration settings forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT company_id INTO v_company FROM public.integrations_config
        WHERE id = p_id FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF NOT app_security.can_manage_catalog(v_company) THEN
        RAISE EXCEPTION 'integration outside scope' USING ERRCODE = '42501';
    END IF;
    DELETE FROM public.integrations_config WHERE id = p_id;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id)
        VALUES (app_security.actor_id(),'integration_deleted','integration',p_id);
    RETURN true;
END;
$function$;

-- Background duplicate detection: same policyholder + insurer + type and
-- (identical policy number OR overlapping subject terms). Candidates are only
-- disclosed when the reader can see BOTH tickets (duplicate_candidates_read).
CREATE FUNCTION app_security.scan_duplicate_candidates(p_limit integer DEFAULT 20)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_inserted integer := 0;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 200 THEN RETURN 0; END IF;
    INSERT INTO public.duplicate_candidates(first_complaint_id,
        first_complaint_created_at, second_complaint_id, second_complaint_created_at,
        confidence)
    SELECT pair.first_id, first_row.created_at, pair.second_id, second_row.created_at,
        pair.confidence
    FROM (
        SELECT LEAST(a.id, b.id) AS first_id, GREATEST(a.id, b.id) AS second_id,
            CASE WHEN a.policy_number = b.policy_number THEN 0.95 ELSE 0.70 END AS confidence
        FROM public.complaints a
        JOIN public.complaints b
          ON a.id < b.id AND a.customer_id = b.customer_id
         AND a.company_id = b.company_id AND a.insurance_type = b.insurance_type
        WHERE a.merged_into_id IS NULL AND b.merged_into_id IS NULL
          AND a.created_at >= now() - interval '90 days'
          AND b.created_at >= now() - interval '90 days'
          AND (a.policy_number = b.policy_number
               OR b.search_vector @@ plainto_tsquery('simple', a.subject)
               OR a.search_vector @@ plainto_tsquery('simple', b.subject))
        ORDER BY a.created_at DESC
        LIMIT p_limit
    ) pair
    JOIN public.complaints first_row ON first_row.id = pair.first_id
    JOIN public.complaints second_row ON second_row.id = pair.second_id
    ON CONFLICT (first_complaint_id, first_complaint_created_at,
                 second_complaint_id, second_complaint_created_at) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    RETURN v_inserted;
END;
$function$;

-- Staff-triggered re-scan for one visible ticket; never scans out of scope.
CREATE FUNCTION app_security.detect_duplicate_candidates(p_id uuid, p_created timestamptz)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_inserted integer := 0;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('complaints.merge') THEN
        RAISE EXCEPTION 'duplicate scan forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_ticket FROM public.complaints
        WHERE id = p_id AND created_at = p_created;
    IF NOT FOUND OR NOT app_security.can_read_complaint(v_ticket.company_id,
            v_ticket.insurance_type, v_ticket.assigned_agent_id, v_ticket.team_id,
            v_ticket.customer_id) THEN
        RETURN 0;
    END IF;
    INSERT INTO public.duplicate_candidates(first_complaint_id,
        first_complaint_created_at, second_complaint_id, second_complaint_created_at,
        confidence)
    SELECT LEAST(candidate.id, v_ticket.id),
        CASE WHEN candidate.id < v_ticket.id THEN candidate.created_at
             ELSE v_ticket.created_at END,
        GREATEST(candidate.id, v_ticket.id),
        CASE WHEN candidate.id < v_ticket.id THEN v_ticket.created_at
             ELSE candidate.created_at END,
        CASE WHEN candidate.policy_number = v_ticket.policy_number THEN 0.95
             ELSE 0.70 END
    FROM public.complaints candidate
    WHERE candidate.id <> v_ticket.id
      AND candidate.merged_into_id IS NULL
      AND candidate.customer_id = v_ticket.customer_id
      AND candidate.company_id = v_ticket.company_id
      AND candidate.insurance_type = v_ticket.insurance_type
      AND candidate.created_at >= v_ticket.created_at - interval '90 days'
      AND candidate.created_at <= v_ticket.created_at + interval '90 days'
      AND app_security.can_read_complaint(candidate.company_id, candidate.insurance_type,
          candidate.assigned_agent_id, candidate.team_id, candidate.customer_id)
      AND (candidate.policy_number = v_ticket.policy_number
           OR candidate.search_vector @@ plainto_tsquery('simple', v_ticket.subject)
           OR v_ticket.search_vector @@ plainto_tsquery('simple', candidate.subject))
    LIMIT 20
    ON CONFLICT (first_complaint_id, first_complaint_created_at,
                 second_complaint_id, second_complaint_created_at) DO NOTHING;
    GET DIAGNOSTICS v_inserted = ROW_COUNT;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'duplicate_scan','complaint',p_id,
            jsonb_build_object('candidates', v_inserted));
    RETURN v_inserted;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.save_assignment_rule(uuid,jsonb),
    app_security.delete_assignment_rule(uuid),
    app_security.preview_assignment(uuid,insurance_type,complaint_priority,uuid,uuid),
    app_security.save_integration(uuid,jsonb),
    app_security.admin_integrations(),
    app_security.delete_integration(uuid),
    app_security.detect_duplicate_candidates(uuid,timestamptz) TO app_runtime;
GRANT EXECUTE ON FUNCTION
    app_security.scan_duplicate_candidates(integer) TO app_worker;
