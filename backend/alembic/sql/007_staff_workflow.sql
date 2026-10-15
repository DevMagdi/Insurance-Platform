-- Phase 3 staff workspace. Existing tables/privileges remain least-privilege.
ALTER TABLE complaints ADD COLUMN merged_into_id uuid;
ALTER TABLE complaints ADD COLUMN merged_into_created_at timestamptz;
ALTER TABLE complaints ADD COLUMN response_due_at timestamptz;
ALTER TABLE complaints ADD COLUMN sla_breached_at timestamptz;
ALTER TABLE complaints ADD CONSTRAINT fk_complaints_merge_target
    FOREIGN KEY (merged_into_id, merged_into_created_at) REFERENCES complaints(id, created_at);
ALTER TABLE complaints ADD CONSTRAINT ck_complaints_merge_pair CHECK
    ((merged_into_id IS NULL) = (merged_into_created_at IS NULL)
     AND (merged_into_id IS NULL OR merged_into_id <> id));
CREATE INDEX ix_complaints_sla_pending ON complaints (sla_due_at, id)
    WHERE sla_due_at IS NOT NULL AND sla_breached_at IS NULL
      AND status <> 'resolved';

-- A rule applies at creation time; the most specific active match wins. Empty
-- business_hours denotes continuous wall-clock time; nonempty schedules are
-- rejected by the admin API until a calendar-aware engine is integrated.
CREATE FUNCTION app_security.set_ticket_sla() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_rule sla_rules%ROWTYPE;
BEGIN
    SELECT * INTO v_rule FROM public.sla_rules s
    WHERE s.is_active
      AND (s.company_id IS NULL OR s.company_id = NEW.company_id)
      AND (s.insurance_type IS NULL OR s.insurance_type = NEW.insurance_type)
      AND (s.priority IS NULL OR s.priority = NEW.priority)
      AND s.business_hours = '{}'::jsonb
    ORDER BY (s.company_id IS NOT NULL)::integer DESC,
        (s.insurance_type IS NOT NULL)::integer DESC,
        (s.priority IS NOT NULL)::integer DESC, s.created_at DESC, s.id
    LIMIT 1;
    IF FOUND THEN
        NEW.response_due_at := NEW.created_at + make_interval(mins => v_rule.response_minutes);
        NEW.sla_due_at := NEW.created_at + make_interval(mins => v_rule.resolution_minutes);
    END IF;
    RETURN NEW;
END;
$function$;
CREATE TRIGGER tr_ticket_sla BEFORE INSERT ON complaints
    FOR EACH ROW EXECUTE FUNCTION app_security.set_ticket_sla();

-- Both the intended assignee's grants AND data scope must cover the ticket
-- AFTER assignment. Own/team scopes are checked against the prospective owner.
CREATE FUNCTION app_security.agent_can_see(
    p_agent uuid, p_company uuid, p_type insurance_type, p_team uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
    SELECT EXISTS (SELECT 1 FROM public.staff_users u WHERE u.id = p_agent
      AND u.status = 'active'
      AND COALESCE((SELECT o.effect = 'allow' FROM public.user_permission_overrides o
          WHERE o.user_id = u.id AND o.permission_code = 'complaints.view'),
          EXISTS (SELECT 1 FROM public.user_roles ur
              JOIN public.role_permissions rp ON rp.role_id = ur.role_id
              WHERE ur.user_id = u.id AND rp.permission_code = 'complaints.view'))
      AND EXISTS (SELECT 1 FROM public.user_company_scope cs
          WHERE cs.user_id = u.id AND (cs.all_companies OR cs.company_id = p_company))
      AND (NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope ts
                      WHERE ts.user_id = u.id)
           OR EXISTS (SELECT 1 FROM public.user_insurance_type_scope ts
                      WHERE ts.user_id = u.id AND ts.insurance_type = p_type))
      AND EXISTS (SELECT 1 FROM public.user_ownership_level own
          WHERE own.user_id = u.id AND
            (own.level IN ('all', 'own') OR
              (own.level = 'team' AND EXISTS (SELECT 1 FROM public.team_members tm
                  WHERE tm.user_id = u.id AND tm.team_id = p_team)))))
$function$;

CREATE FUNCTION app_security.staff_assign(
    p_id uuid, p_created timestamptz, p_agent uuid, p_team uuid DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_status complaint_status;
BEGIN
    SELECT * INTO v_ticket FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR v_ticket.merged_into_id IS NOT NULL
       OR v_ticket.status = 'resolved'
       OR NOT app_security.can_read_complaint(v_ticket.company_id,
            v_ticket.insurance_type, v_ticket.assigned_agent_id,
            v_ticket.team_id, v_ticket.customer_id)
       OR NOT app_security.has_permission(CASE WHEN v_ticket.assigned_agent_id IS NULL
              THEN 'complaints.assign' ELSE 'complaints.reassign' END)
       OR (v_ticket.assigned_agent_id = p_agent AND v_ticket.team_id IS NOT DISTINCT FROM p_team)
       OR NOT app_security.agent_can_see(p_agent, v_ticket.company_id,
            v_ticket.insurance_type, p_team)
       OR (p_team IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.team_members tm
              WHERE tm.team_id = p_team AND tm.user_id = p_agent)) THEN
        RETURN false;
    END IF;
    v_status := CASE WHEN v_ticket.status IN ('new','reopened')
        THEN 'assigned'::complaint_status ELSE v_ticket.status END;
    UPDATE public.complaints SET assigned_agent_id = p_agent,
        team_id = p_team, status = v_status, updated_at = now()
        WHERE id = p_id AND created_at = p_created;
    INSERT INTO public.complaint_events
        (complaint_id, complaint_created_at, event_type, from_status,
         to_status, actor_staff_id, metadata)
        VALUES (p_id, p_created, 'assigned', v_ticket.status, v_status,
            app_security.actor_id(), jsonb_build_object('agent_id', p_agent));
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type,
        resource_id, complaint_id, complaint_created_at, before_state, after_state)
        VALUES (app_security.actor_id(), 'assigned', 'complaint', p_id,
            p_id, p_created,
            jsonb_build_object('agent_id', v_ticket.assigned_agent_id),
            jsonb_build_object('agent_id', p_agent));
    INSERT INTO public.notifications
        (recipient_staff_id, complaint_id, complaint_created_at, kind, payload)
        VALUES (p_agent, p_id, p_created, 'assigned',
            jsonb_build_object('ticket_no', v_ticket.ticket_no));
    IF v_status IS DISTINCT FROM v_ticket.status THEN
        INSERT INTO public.notification_outbox
            (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
            VALUES (p_id, p_created, v_ticket.customer_id, 'status_changed',
                jsonb_build_object('ticket_no', v_ticket.ticket_no,
                    'status', v_status::text));
        INSERT INTO public.notifications
            (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
            VALUES (v_ticket.customer_id, p_id, p_created, 'status_changed',
                jsonb_build_object('ticket_no', v_ticket.ticket_no,
                    'status', v_status::text));
    END IF;
    RETURN true;
END;
$function$;

-- Preserve the Phase 2 fast-close path (new -> resolved) while explicitly
-- forbidding nonsensical transitions and reserving transfer for escalation.
CREATE OR REPLACE FUNCTION app_security.staff_change_status(
    p_id uuid, p_created timestamptz, p_status complaint_status)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE;
BEGIN
    SELECT * INTO v_ticket FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR NOT app_security.has_permission('complaints.change_status')
       OR NOT app_security.can_read_complaint(v_ticket.company_id,
            v_ticket.insurance_type, v_ticket.assigned_agent_id,
            v_ticket.team_id, v_ticket.customer_id)
       OR v_ticket.merged_into_id IS NOT NULL OR v_ticket.status = p_status
       OR NOT ((v_ticket.status = 'new' AND
                p_status IN ('assigned', 'in_review', 'resolved'))
            OR (v_ticket.status = 'assigned' AND
                p_status IN ('in_review', 'resolved'))
            OR (v_ticket.status = 'in_review' AND p_status = 'resolved')
            OR (v_ticket.status = 'reopened' AND
                p_status IN ('assigned', 'in_review', 'resolved'))
            OR (v_ticket.status = 'resolved' AND p_status = 'in_review')
            OR (v_ticket.status = 'transferred' AND
                p_status IN ('in_review', 'resolved'))) THEN
        RETURN false;
    END IF;
    UPDATE public.complaints SET status = p_status,
        resolved_at = CASE WHEN p_status = 'resolved' THEN now() ELSE NULL END,
        updated_at = now() WHERE id = p_id AND created_at = p_created;
    INSERT INTO public.complaint_events
        (complaint_id, complaint_created_at, event_type, from_status,
         to_status, actor_staff_id)
        VALUES (p_id, p_created, 'status_changed', v_ticket.status,
            p_status, app_security.actor_id());
    PERFORM app_security.queue_customer_notice(p_id, p_created, 'status_changed');
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type,
        resource_id, complaint_id, complaint_created_at, before_state, after_state)
        VALUES (app_security.actor_id(), 'status_changed', 'complaint', p_id,
            p_id, p_created, jsonb_build_object('status', v_ticket.status),
            jsonb_build_object('status', p_status));
    RETURN true;
END;
$function$;

-- Escalation is a restricted status transition, not a fake delivered email.
-- Queued insurer delivery is handled by the restricted worker (next revision).
CREATE TABLE insurer_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    recipient_email text NOT NULL,
    ticket_no text NOT NULL,
    reason text NOT NULL,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','processing','sent','pending_provider','failed')),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints(id, created_at) ON DELETE CASCADE,
    CHECK (recipient_email ~ '^[^@ ]+@[^@ ]+\.[^@ ]+$')
);
CREATE INDEX ix_insurer_outbox_due ON insurer_outbox (next_attempt_at, id)
    WHERE state IN ('queued', 'processing');
ALTER TABLE insurer_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE insurer_outbox FORCE ROW LEVEL SECURITY;

CREATE FUNCTION app_security.staff_escalate(
    p_id uuid, p_created timestamptz, p_reason text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_email text;
BEGIN
    SELECT * INTO v_ticket FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR NOT app_security.has_permission('complaints.escalate')
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

CREATE FUNCTION app_security.staff_merge(
    p_source uuid, p_source_created timestamptz,
    p_target uuid, p_target_created timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_source complaints%ROWTYPE; v_target complaints%ROWTYPE;
BEGIN
    IF p_source = p_target OR NOT app_security.has_permission('complaints.merge') THEN
        RETURN false;
    END IF;
    -- Deterministic lock order prevents opposing merges deadlocking.
    PERFORM c.id FROM public.complaints c
        WHERE (c.id = p_source AND c.created_at = p_source_created)
           OR (c.id = p_target AND c.created_at = p_target_created)
        ORDER BY c.id FOR UPDATE;
    SELECT * INTO v_source FROM public.complaints
        WHERE id = p_source AND created_at = p_source_created;
    SELECT * INTO v_target FROM public.complaints
        WHERE id = p_target AND created_at = p_target_created;
    IF v_source.id IS NULL OR v_target.id IS NULL
       OR v_source.merged_into_id IS NOT NULL OR v_target.merged_into_id IS NOT NULL
       OR v_source.status = 'resolved'
       OR v_source.customer_id <> v_target.customer_id
       OR v_source.company_id <> v_target.company_id
       OR v_source.insurance_type <> v_target.insurance_type
       OR NOT app_security.can_read_complaint(v_source.company_id,
            v_source.insurance_type, v_source.assigned_agent_id,
            v_source.team_id, v_source.customer_id)
       OR NOT app_security.can_read_complaint(v_target.company_id,
            v_target.insurance_type, v_target.assigned_agent_id,
            v_target.team_id, v_target.customer_id) THEN
        RETURN false;
    END IF;
    UPDATE public.complaints SET merged_into_id = p_target,
        merged_into_created_at = p_target_created, status = 'resolved',
        resolved_at = now(), updated_at = now()
        WHERE id = p_source AND created_at = p_source_created;
    INSERT INTO public.complaint_events
        (complaint_id, complaint_created_at, event_type, from_status,
         to_status, actor_staff_id, metadata)
        VALUES (p_source, p_source_created, 'merged', v_source.status,
            'resolved', app_security.actor_id(),
            jsonb_build_object('target_ticket_no', v_target.ticket_no));
    INSERT INTO public.complaint_events
        (complaint_id, complaint_created_at, event_type, actor_staff_id,
         visibility, metadata)
        VALUES (p_target, p_target_created, 'merge_target',
            app_security.actor_id(), 'internal',
            jsonb_build_object('source_ticket_no', v_source.ticket_no));
    INSERT INTO public.audit_logs
        (actor_staff_id, action, resource_type, resource_id,
         complaint_id, complaint_created_at, after_state)
        VALUES (app_security.actor_id(), 'merged', 'complaint', p_source,
            p_source, p_source_created, jsonb_build_object('target_id', p_target));
    INSERT INTO public.notification_outbox
        (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
        VALUES (p_source, p_source_created, v_source.customer_id, 'status_changed',
            jsonb_build_object('ticket_no', v_source.ticket_no, 'status', 'resolved'));
    INSERT INTO public.notifications
        (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
        VALUES (v_source.customer_id, p_source, p_source_created, 'status_changed',
            jsonb_build_object('ticket_no', v_source.ticket_no, 'status', 'resolved'));
    INSERT INTO public.duplicate_candidates
        (first_complaint_id, first_complaint_created_at,
         second_complaint_id, second_complaint_created_at,
         confidence, decision, reviewed_by_staff_id)
        VALUES (LEAST(p_source, p_target),
            CASE WHEN p_source < p_target THEN p_source_created ELSE p_target_created END,
            GREATEST(p_source, p_target),
            CASE WHEN p_source > p_target THEN p_source_created ELSE p_target_created END,
            1, 'confirmed', app_security.actor_id())
        ON CONFLICT (first_complaint_id, first_complaint_created_at,
                     second_complaint_id, second_complaint_created_at)
        DO UPDATE SET decision = 'confirmed', reviewed_by_staff_id = app_security.actor_id();
    RETURN true;
END;
$function$;

-- Saved views store validated filter JSON. Reading a saved view ALWAYS runs
-- a fresh scoped query; ownership of a view is not authority over its results.
ALTER TABLE saved_filters ENABLE ROW LEVEL SECURITY;
ALTER TABLE saved_filters FORCE ROW LEVEL SECURITY;
CREATE POLICY saved_views_owner ON saved_filters FOR ALL TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND owner_staff_id = app_security.actor_id()
        AND app_security.has_permission('complaints.view'))
    WITH CHECK (app_security.actor_scope() = 'staff'
        AND owner_staff_id = app_security.actor_id()
        AND app_security.has_permission('complaints.view'));
GRANT SELECT, DELETE ON saved_filters TO app_runtime;
GRANT INSERT (owner_staff_id, name, criteria) ON saved_filters TO app_runtime;
GRANT UPDATE (name, criteria) ON saved_filters TO app_runtime;

-- Read-receipt updates cannot be used to mark someone else's ticket or a
-- notification made inaccessible by subsequent scope revocation.
CREATE POLICY notifications_mark_read ON notifications FOR UPDATE TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND recipient_staff_id = app_security.actor_id()
        AND (complaint_id IS NULL OR
             app_security.can_read_complaint_id(complaint_id, complaint_created_at)))
    WITH CHECK (app_security.actor_scope() = 'staff'
        AND recipient_staff_id = app_security.actor_id()
        AND (complaint_id IS NULL OR
             app_security.can_read_complaint_id(complaint_id, complaint_created_at)));
GRANT UPDATE (read_at) ON notifications TO app_runtime;

-- The migrator owns all new definer functions; app_runtime can execute only
-- guarded staff functions, not SLA trigger internals or worker jobs.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.staff_assign(uuid, timestamptz, uuid, uuid),
    app_security.staff_escalate(uuid, timestamptz, text),
    app_security.staff_merge(uuid, timestamptz, uuid, timestamptz)
    TO app_runtime;
