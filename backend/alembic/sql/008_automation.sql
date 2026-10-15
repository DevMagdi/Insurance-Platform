-- Automatic assignment is opt-in per active rule. No rule means no assignment.
ALTER TABLE assignment_rules ADD COLUMN strategy text NOT NULL
    DEFAULT 'least_workload' CHECK (strategy IN ('round_robin','least_workload','by_company'));
ALTER TABLE assignment_rules ADD COLUMN last_assigned_agent_id uuid
    REFERENCES staff_users(id) ON DELETE SET NULL;

CREATE FUNCTION app_security.auto_assign_tickets(p_limit integer DEFAULT 20)
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
        -- Lock the chosen rule: competing worker jobs cannot race its cursor.
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
        UPDATE public.assignment_rules SET last_assigned_agent_id = v_agent
            WHERE id = v_rule.id;
        UPDATE public.complaints SET assigned_agent_id = v_agent, team_id = v_team,
            status = 'assigned', updated_at = now()
            WHERE id = v_ticket.id AND created_at = v_ticket.created_at;
        INSERT INTO public.complaint_events
            (complaint_id, complaint_created_at, event_type, from_status,
             to_status, metadata)
            VALUES (v_ticket.id, v_ticket.created_at, 'auto_assigned', 'new',
                'assigned', jsonb_build_object('rule_id', v_rule.id));
        INSERT INTO public.audit_logs
            (action, resource_type, resource_id, complaint_id,
             complaint_created_at, after_state)
            VALUES ('auto_assigned', 'complaint', v_ticket.id, v_ticket.id,
                v_ticket.created_at, jsonb_build_object('agent_id', v_agent));
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

-- Timed breach processing is worker-only. It uses row locks to prevent
-- duplicate alerts/escalations; no runtime table grants or global export.
CREATE FUNCTION app_security.process_sla_breaches(p_limit integer DEFAULT 20)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_email text; v_done integer := 0;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN 0; END IF;
    FOR v_ticket IN
        SELECT c.* FROM public.complaints c
        WHERE c.sla_due_at <= now() AND c.sla_breached_at IS NULL
          AND c.status <> 'resolved' AND c.merged_into_id IS NULL
        ORDER BY c.sla_due_at, c.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        SELECT email INTO v_email FROM public.insurance_companies co,
            LATERAL unnest(co.contact_emails) AS email
            WHERE co.id = v_ticket.company_id
              AND email ~ '^[^@ ]+@[^@ ]+\.[^@ ]+$'
            LIMIT 1;
        UPDATE public.complaints SET sla_breached_at = now(),
            status = CASE WHEN v_email IS NOT NULL THEN 'transferred'::complaint_status
                ELSE status END, updated_at = now()
            WHERE id = v_ticket.id AND created_at = v_ticket.created_at;
        INSERT INTO public.complaint_events
            (complaint_id, complaint_created_at, event_type, from_status,
             to_status, metadata)
            VALUES (v_ticket.id, v_ticket.created_at, 'sla_breached',
                v_ticket.status, CASE WHEN v_email IS NOT NULL
                    THEN 'transferred'::complaint_status ELSE v_ticket.status END,
                jsonb_build_object('due_at', v_ticket.sla_due_at));
        INSERT INTO public.audit_logs
            (action, resource_type, resource_id, complaint_id,
             complaint_created_at, after_state)
            VALUES ('sla_breached', 'complaint', v_ticket.id, v_ticket.id,
                v_ticket.created_at, jsonb_build_object('due_at', v_ticket.sla_due_at,
                    'auto_escalated', v_email IS NOT NULL));
        IF v_ticket.assigned_agent_id IS NOT NULL THEN
            INSERT INTO public.notifications
                (recipient_staff_id, complaint_id, complaint_created_at, kind, payload)
                VALUES (v_ticket.assigned_agent_id, v_ticket.id, v_ticket.created_at,
                    'sla_breached', jsonb_build_object('ticket_no', v_ticket.ticket_no));
        END IF;
        IF v_email IS NOT NULL THEN
            INSERT INTO public.insurer_outbox
                (complaint_id, complaint_created_at, recipient_email, ticket_no, reason)
                VALUES (v_ticket.id, v_ticket.created_at, v_email,
                    v_ticket.ticket_no, 'SLA resolution deadline exceeded');
            INSERT INTO public.notifications
                (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
                VALUES (v_ticket.customer_id, v_ticket.id, v_ticket.created_at,
                    'status_changed', jsonb_build_object('ticket_no', v_ticket.ticket_no,
                        'status', 'transferred'));
            INSERT INTO public.notification_outbox
                (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
                VALUES (v_ticket.id, v_ticket.created_at, v_ticket.customer_id,
                    'status_changed', jsonb_build_object('ticket_no', v_ticket.ticket_no,
                        'status', 'transferred'));
        END IF;
        v_done := v_done + 1;
    END LOOP;
    RETURN v_done;
END;
$function$;

CREATE FUNCTION app_security.claim_insurer_notices(p_limit integer DEFAULT 20)
RETURNS TABLE (notice_id uuid, recipient_email text, ticket_no text, reason text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    RETURN QUERY WITH claim AS (
        SELECT i.id FROM public.insurer_outbox i
        WHERE i.state IN ('queued','processing') AND i.attempts < 10
          AND i.next_attempt_at <= now()
        ORDER BY i.next_attempt_at, i.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    ), updated AS (
        UPDATE public.insurer_outbox i SET state = 'processing',
            attempts = i.attempts + 1, next_attempt_at = now() + interval '5 minutes'
            FROM claim WHERE i.id = claim.id
        RETURNING i.id, i.recipient_email, i.ticket_no, i.reason
    ) SELECT u.id, u.recipient_email, u.ticket_no, u.reason FROM updated u;
END;
$function$;

CREATE FUNCTION app_security.finish_insurer_notice(p_id uuid, p_state text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_state NOT IN ('sent','pending_provider','failed','retry') THEN RETURN false; END IF;
    UPDATE public.insurer_outbox SET
        state = CASE WHEN p_state = 'retry' THEN
            CASE WHEN attempts >= 10 THEN 'failed' ELSE 'queued' END ELSE p_state END,
        next_attempt_at = CASE WHEN p_state = 'retry' THEN
            now() + make_interval(secs => LEAST(3600, (2 ^ attempts)::integer * 30))
            ELSE next_attempt_at END,
        processed_at = CASE WHEN p_state = 'retry' THEN NULL ELSE now() END
        WHERE id = p_id AND state = 'processing';
    RETURN FOUND;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.auto_assign_tickets(integer),
    app_security.process_sla_breaches(integer),
    app_security.claim_insurer_notices(integer),
    app_security.finish_insurer_notice(uuid, text) TO app_worker;
