-- Business-hours-aware SLA: validated weekly calendars with holiday lists,
-- alert thresholds, first-response tracking and an at-risk notifier.
-- An empty business_hours object keeps the continuous wall-clock behaviour
-- introduced by revision 007, so existing rules and deadlines do not change.
ALTER TABLE sla_rules ADD COLUMN alert_before_minutes integer NOT NULL DEFAULT 60;
ALTER TABLE sla_rules ADD CONSTRAINT ck_sla_alert_before
    CHECK (alert_before_minutes BETWEEN 0 AND 43200);
-- Legacy rows may already violate the ordering; the check still governs new writes.
ALTER TABLE sla_rules ADD CONSTRAINT ck_sla_window
    CHECK (resolution_minutes >= response_minutes) NOT VALID;

ALTER TABLE complaints ADD COLUMN sla_alert_at timestamptz;
ALTER TABLE complaints ADD COLUMN sla_alerted_at timestamptz;
ALTER TABLE complaints ADD COLUMN first_response_at timestamptz;
ALTER TABLE complaints ADD COLUMN response_breached_at timestamptz;
CREATE INDEX ix_complaints_sla_alerts ON complaints (sla_alert_at, id)
    WHERE sla_alert_at IS NOT NULL AND sla_alerted_at IS NULL AND status <> 'resolved';
CREATE INDEX ix_complaints_response_pending ON complaints (response_due_at, id)
    WHERE response_due_at IS NOT NULL AND response_breached_at IS NULL
      AND status <> 'resolved';

-- A calendar is a small, strictly validated document:
--   {"time_zone": "Africa/Cairo",
--    "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
--    "holidays": ["2026-01-07"]}
-- weekday follows PostgreSQL extract(DOW): 0 = Sunday. Multiple spans per day are
-- allowed. Invalid documents are refused at write time AND ignored at read time.
CREATE FUNCTION app_security.valid_business_hours(p_calendar jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_item jsonb;
BEGIN
    IF p_calendar IS NULL OR jsonb_typeof(p_calendar) <> 'object' THEN RETURN false; END IF;
    IF p_calendar = '{}'::jsonb THEN RETURN true; END IF;  -- continuous 24/7
    IF p_calendar - ARRAY['time_zone','weekly','holidays'] <> '{}'::jsonb THEN RETURN false; END IF;
    IF COALESCE(jsonb_typeof(p_calendar->'time_zone'), 'missing') <> 'string'
       OR p_calendar->>'time_zone' = ''
       OR NOT EXISTS (SELECT 1 FROM pg_timezone_names z WHERE z.name = p_calendar->>'time_zone')
    THEN RETURN false; END IF;
    IF COALESCE(jsonb_typeof(p_calendar->'weekly'), 'missing') <> 'array'
       OR COALESCE(jsonb_array_length(p_calendar->'weekly'), 0) NOT BETWEEN 1 AND 21
    THEN RETURN false; END IF;
    FOR v_item IN SELECT value FROM jsonb_array_elements(p_calendar->'weekly') LOOP
        IF jsonb_typeof(v_item) <> 'object'
           OR v_item - ARRAY['weekday','start','end'] <> '{}'::jsonb
           OR (v_item->>'weekday') !~ '^[0-6]$'
           OR (v_item->>'start') !~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
           OR (v_item->>'end') !~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'
           OR (v_item->>'end') <= (v_item->>'start')
        THEN RETURN false; END IF;
    END LOOP;
    IF p_calendar ? 'holidays' THEN
        IF jsonb_typeof(p_calendar->'holidays') <> 'array'
           OR jsonb_array_length(p_calendar->'holidays') > 60 THEN RETURN false; END IF;
        FOR v_item IN SELECT value FROM jsonb_array_elements(p_calendar->'holidays') LOOP
            IF jsonb_typeof(v_item) <> 'string'
               OR (v_item #>> '{}') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN RETURN false; END IF;
        END LOOP;
    END IF;
    RETURN true;
END;
$function$;

-- Consumes p_minutes of WORKING time starting at p_start (inclusive), skipping
-- holidays/non-working weekdays and moving forward to the next span. Returns
-- NULL when the calendar cannot satisfy the request inside the search bound.
CREATE FUNCTION app_security.add_business_minutes(
    p_start timestamptz, p_minutes integer, p_calendar jsonb
) RETURNS timestamptz
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_zone text; v_cursor timestamptz; v_remaining integer; v_day date;
    v_weekday integer; v_span_start timestamptz; v_span_end timestamptz;
    v_available integer; v_span record; i integer;
BEGIN
    IF p_start IS NULL OR p_minutes IS NULL OR p_minutes < 0 THEN RETURN NULL; END IF;
    IF p_calendar IS NULL OR p_calendar = '{}'::jsonb THEN
        RETURN p_start + make_interval(mins => p_minutes);
    END IF;
    IF NOT app_security.valid_business_hours(p_calendar) THEN RETURN NULL; END IF;
    IF p_minutes = 0 THEN RETURN p_start; END IF;
    v_zone := p_calendar->>'time_zone';
    v_cursor := p_start;
    v_remaining := p_minutes;
    -- Bound the walk: ~5.5 years of calendar time covers even a one-year
    -- business-minute budget on a two-day working week.
    FOR i IN 0..2000 LOOP
        v_day := (v_cursor AT TIME ZONE v_zone)::date;
        IF NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(
                COALESCE(p_calendar->'holidays', '[]'::jsonb)) AS h
            WHERE h = to_char(v_day, 'YYYY-MM-DD')
        ) THEN
            v_weekday := EXTRACT(DOW FROM (v_cursor AT TIME ZONE v_zone))::integer;
            FOR v_span IN
                SELECT e->>'start' AS span_start, e->>'end' AS span_end
                FROM jsonb_array_elements(p_calendar->'weekly') AS e
                WHERE (e->>'weekday')::integer = v_weekday
                ORDER BY e->>'start'
            LOOP
                v_span_start := (v_day + v_span.span_start::time)::timestamp AT TIME ZONE v_zone;
                v_span_end := (v_day + v_span.span_end::time)::timestamp AT TIME ZONE v_zone;
                IF v_span_end <= v_cursor THEN CONTINUE; END IF;
                v_available := floor(EXTRACT(EPOCH FROM
                    (v_span_end - GREATEST(v_cursor, v_span_start))) / 60)::integer;
                IF v_available <= 0 THEN CONTINUE; END IF;
                IF v_remaining <= v_available THEN
                    RETURN GREATEST(v_cursor, v_span_start) + make_interval(mins => v_remaining);
                END IF;
                v_remaining := v_remaining - v_available;
                v_cursor := v_span_end;
            END LOOP;
        END IF;
        v_cursor := GREATEST(v_cursor, (v_day + 1)::timestamp AT TIME ZONE v_zone);
    END LOOP;
    RETURN NULL;
END;
$function$;

-- Most specific active rule wins: company, then insurance type, then priority.
CREATE FUNCTION app_security.sla_match(
    p_company uuid, p_type insurance_type, p_priority complaint_priority
) RETURNS sla_rules
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT * FROM public.sla_rules s
    WHERE s.is_active
      AND (s.company_id IS NULL OR s.company_id = p_company)
      AND (s.insurance_type IS NULL OR s.insurance_type = p_type)
      AND (s.priority IS NULL OR s.priority = p_priority)
    ORDER BY (s.company_id IS NOT NULL)::integer DESC,
        (s.insurance_type IS NOT NULL)::integer DESC,
        (s.priority IS NOT NULL)::integer DESC, s.created_at DESC, s.id
    LIMIT 1
$function$;

-- Replace the revision 007 trigger body: calendars are now honoured and the
-- at-risk deadline is materialized so the notifier can use a partial index.
CREATE OR REPLACE FUNCTION app_security.set_ticket_sla() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_rule sla_rules%ROWTYPE;
BEGIN
    SELECT * INTO v_rule FROM public.sla_rules s
    WHERE s.is_active
      AND (s.company_id IS NULL OR s.company_id = NEW.company_id)
      AND (s.insurance_type IS NULL OR s.insurance_type = NEW.insurance_type)
      AND (s.priority IS NULL OR s.priority = NEW.priority)
    ORDER BY (s.company_id IS NOT NULL)::integer DESC,
        (s.insurance_type IS NOT NULL)::integer DESC,
        (s.priority IS NOT NULL)::integer DESC, s.created_at DESC, s.id
    LIMIT 1;
    IF FOUND THEN
        NEW.response_due_at := app_security.add_business_minutes(
            NEW.created_at, v_rule.response_minutes, v_rule.business_hours);
        NEW.sla_due_at := app_security.add_business_minutes(
            NEW.created_at, v_rule.resolution_minutes, v_rule.business_hours);
        IF NEW.response_due_at IS NOT NULL AND NEW.sla_due_at IS NOT NULL
           AND NEW.response_due_at > NEW.sla_due_at THEN
            NEW.response_due_at := NEW.sla_due_at;
        END IF;
        IF NEW.sla_due_at IS NOT NULL THEN
            NEW.sla_alert_at := app_security.add_business_minutes(
                NEW.created_at,
                GREATEST(v_rule.resolution_minutes - v_rule.alert_before_minutes, 0),
                v_rule.business_hours);
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

-- The first PUBLIC staff reply stops the response clock. Internal notes do not.
CREATE FUNCTION app_security.set_first_staff_response() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NEW.sender_staff_id IS NOT NULL AND NEW.visibility = 'public' THEN
        UPDATE public.complaints SET first_response_at = NEW.created_at
            WHERE id = NEW.complaint_id AND created_at = NEW.complaint_created_at
              AND first_response_at IS NULL;
    END IF;
    RETURN NULL;
END;
$function$;
CREATE TRIGGER tr_first_staff_response AFTER INSERT ON complaint_messages
    FOR EACH ROW EXECUTE FUNCTION app_security.set_first_staff_response();

-- Backfill the new column from the immutable message log.
UPDATE public.complaints c SET first_response_at = r.first_at
FROM (
    SELECT m.complaint_id, m.complaint_created_at, min(m.created_at) AS first_at
    FROM public.complaint_messages m
    WHERE m.sender_staff_id IS NOT NULL AND m.visibility = 'public'
    GROUP BY m.complaint_id, m.complaint_created_at
) r
WHERE c.id = r.complaint_id AND c.created_at = r.complaint_created_at
  AND c.first_response_at IS NULL;

-- Approaching-breach alerts. Worker-only; marks each ticket once and notifies
-- the current assignee (an unassigned ticket still records the at-risk event).
CREATE FUNCTION app_security.process_sla_warnings(p_limit integer DEFAULT 20)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_done integer := 0;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN 0; END IF;
    FOR v_ticket IN
        SELECT c.* FROM public.complaints c
        WHERE c.sla_alert_at IS NOT NULL AND c.sla_alerted_at IS NULL
          AND c.sla_alert_at <= now() AND c.sla_breached_at IS NULL
          AND c.status <> 'resolved' AND c.merged_into_id IS NULL
        ORDER BY c.sla_alert_at, c.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE public.complaints SET sla_alerted_at = now(), updated_at = now()
            WHERE id = v_ticket.id AND created_at = v_ticket.created_at;
        INSERT INTO public.complaint_events
            (complaint_id, complaint_created_at, event_type, from_status,
             to_status, metadata)
            VALUES (v_ticket.id, v_ticket.created_at, 'sla_at_risk',
                v_ticket.status, v_ticket.status,
                jsonb_build_object('due_at', v_ticket.sla_due_at));
        IF v_ticket.assigned_agent_id IS NOT NULL THEN
            INSERT INTO public.notifications
                (recipient_staff_id, complaint_id, complaint_created_at, kind, payload)
                VALUES (v_ticket.assigned_agent_id, v_ticket.id, v_ticket.created_at,
                    'sla_at_risk', jsonb_build_object('ticket_no', v_ticket.ticket_no,
                        'due_at', v_ticket.sla_due_at));
        END IF;
        v_done := v_done + 1;
    END LOOP;
    RETURN v_done;
END;
$function$;

-- Replace the revision 008 breach sweep: response breaches are recorded before
-- the (unchanged) resolution sweep that can auto-transfer to the insurer.
CREATE OR REPLACE FUNCTION app_security.process_sla_breaches(p_limit integer DEFAULT 20)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_ticket complaints%ROWTYPE; v_email text; v_done integer := 0;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN 0; END IF;
    FOR v_ticket IN
        SELECT c.* FROM public.complaints c
        WHERE c.response_due_at IS NOT NULL AND c.response_breached_at IS NULL
          AND c.first_response_at IS NULL AND c.response_due_at <= now()
          AND c.status <> 'resolved' AND c.merged_into_id IS NULL
        ORDER BY c.response_due_at, c.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        UPDATE public.complaints SET response_breached_at = now(), updated_at = now()
            WHERE id = v_ticket.id AND created_at = v_ticket.created_at;
        INSERT INTO public.complaint_events
            (complaint_id, complaint_created_at, event_type, from_status,
             to_status, metadata)
            VALUES (v_ticket.id, v_ticket.created_at, 'sla_response_breached',
                v_ticket.status, v_ticket.status,
                jsonb_build_object('due_at', v_ticket.response_due_at));
        INSERT INTO public.audit_logs
            (action, resource_type, resource_id, complaint_id,
             complaint_created_at, after_state)
            VALUES ('sla_response_breached', 'complaint', v_ticket.id, v_ticket.id,
                v_ticket.created_at, jsonb_build_object('due_at', v_ticket.response_due_at));
        IF v_ticket.assigned_agent_id IS NOT NULL THEN
            INSERT INTO public.notifications
                (recipient_staff_id, complaint_id, complaint_created_at, kind, payload)
                VALUES (v_ticket.assigned_agent_id, v_ticket.id, v_ticket.created_at,
                    'sla_response_breached', jsonb_build_object('ticket_no', v_ticket.ticket_no,
                        'due_at', v_ticket.response_due_at));
        END IF;
        v_done := v_done + 1;
    END LOOP;
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

-- Replace the revision 010 guard: calendars are now validated instead of
-- rejected, alert thresholds are bounded and ordering is enforced on new rules.
CREATE OR REPLACE FUNCTION app_security.save_sla_rule(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_prior uuid;
    v_type insurance_type; v_old_type insurance_type; v_calendar jsonb;
BEGIN
    v_company := NULLIF(p_data->>'company_id', '')::uuid;
    v_calendar := COALESCE(p_data->'business_hours', '{}'::jsonb);
    IF jsonb_typeof(p_data) <> 'object'
       OR p_data - ARRAY['company_id','insurance_type','priority','response_minutes',
            'resolution_minutes','business_hours','alert_before_minutes','is_active']
            <> '{}'::jsonb THEN
        RAISE EXCEPTION 'SLA data forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_data->>'insurance_type' NOT IN ('health','general','both')
       AND p_data->>'insurance_type' IS NOT NULL THEN
        RAISE EXCEPTION 'unknown insurance type' USING ERRCODE = '22023';
    END IF;
    v_type := (p_data->>'insurance_type')::insurance_type;
    IF NOT app_security.has_permission('sla.manage')
       OR NOT app_security.can_manage_catalog(v_company, v_type)
       OR ((p_data->>'priority') NOT IN ('low','normal','high','urgent')
           AND p_data->>'priority' IS NOT NULL)
       OR (p_data->>'response_minutes')::integer NOT BETWEEN 1 AND 525600
       OR (p_data->>'resolution_minutes')::integer NOT BETWEEN 1 AND 525600
       OR (p_data->>'resolution_minutes')::integer < (p_data->>'response_minutes')::integer
       OR (p_data->>'alert_before_minutes')::integer NOT BETWEEN 0 AND 43200
       OR NOT app_security.valid_business_hours(v_calendar)
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR (v_company IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.insurance_companies WHERE id = v_company AND is_active)) THEN
        RAISE EXCEPTION 'SLA data forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NOT NULL THEN
        SELECT company_id, insurance_type INTO v_prior, v_old_type
            FROM public.sla_rules WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_prior IS DISTINCT FROM v_company OR
           NOT app_security.can_manage_catalog(v_prior, v_old_type) THEN
            RAISE EXCEPTION 'SLA cannot move across scope' USING ERRCODE = '42501';
        END IF;
        UPDATE public.sla_rules SET insurance_type = v_type,
            priority = (p_data->>'priority')::complaint_priority,
            response_minutes = (p_data->>'response_minutes')::integer,
            resolution_minutes = (p_data->>'resolution_minutes')::integer,
            alert_before_minutes = (p_data->>'alert_before_minutes')::integer,
            business_hours = v_calendar,
            is_active = (p_data->>'is_active')::boolean WHERE id = p_id;
        v_id := p_id;
    ELSE
        INSERT INTO public.sla_rules(company_id,insurance_type,priority,
            response_minutes,resolution_minutes,alert_before_minutes,
            business_hours,is_active)
        VALUES (v_company,v_type,(p_data->>'priority')::complaint_priority,
            (p_data->>'response_minutes')::integer,
            (p_data->>'resolution_minutes')::integer,
            (p_data->>'alert_before_minutes')::integer, v_calendar,
            (p_data->>'is_active')::boolean) RETURNING id INTO v_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'sla_saved','sla_rule',v_id,
            jsonb_build_object('company_id',v_company,'insurance_type',v_type,
                'response_minutes',p_data->>'response_minutes',
                'resolution_minutes',p_data->>'resolution_minutes',
                'alert_before_minutes',p_data->>'alert_before_minutes',
                'calendar', v_calendar <> '{}'::jsonb));
    RETURN v_id;
END;
$function$;

-- Deadline preview for the matched rule. NULL means "not permitted"; a JSON
-- object with matched=false means no rule matched the given ticket shape.
CREATE FUNCTION app_security.simulate_sla(
    p_company uuid, p_type insurance_type, p_priority complaint_priority,
    p_created timestamptz)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_rule sla_rules%ROWTYPE;
BEGIN
    IF NOT app_security.has_permission('sla.manage')
       OR NOT app_security.can_manage_catalog(p_company, p_type)
       OR p_created IS NULL THEN
        RETURN NULL;
    END IF;
    v_rule := app_security.sla_match(p_company, p_type, p_priority);
    IF v_rule.id IS NULL THEN RETURN jsonb_build_object('matched', false); END IF;
    RETURN jsonb_build_object(
        'matched', true,
        'rule_id', v_rule.id,
        'continuous', v_rule.business_hours = '{}'::jsonb,
        'alert_before_minutes', v_rule.alert_before_minutes,
        'business_hours', v_rule.business_hours,
        'response_due_at', app_security.add_business_minutes(
            p_created, v_rule.response_minutes, v_rule.business_hours),
        'sla_due_at', app_security.add_business_minutes(
            p_created, v_rule.resolution_minutes, v_rule.business_hours),
        'alert_at', app_security.add_business_minutes(
            p_created,
            GREATEST(v_rule.resolution_minutes - v_rule.alert_before_minutes, 0),
            v_rule.business_hours));
END;
$function$;

-- Validate a draft calendar and preview the resulting deadline before saving.
CREATE FUNCTION app_security.test_business_hours(
    p_calendar jsonb, p_minutes integer, p_from timestamptz,
    p_company uuid DEFAULT NULL, p_type insurance_type DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_valid boolean; v_due timestamptz;
BEGIN
    IF NOT app_security.has_permission('sla.manage')
       OR NOT app_security.can_manage_catalog(p_company, p_type)
       OR p_minutes IS NULL OR p_minutes NOT BETWEEN 1 AND 525600
       OR p_from IS NULL THEN
        RETURN NULL;
    END IF;
    v_valid := app_security.valid_business_hours(COALESCE(p_calendar, '{}'::jsonb));
    IF v_valid THEN
        v_due := app_security.add_business_minutes(p_from, p_minutes,
            COALESCE(p_calendar, '{}'::jsonb));
    END IF;
    RETURN jsonb_build_object('valid', v_valid, 'due_at', v_due,
        'continuous', COALESCE(p_calendar, '{}'::jsonb) = '{}'::jsonb);
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.save_sla_rule(uuid,jsonb),
    app_security.simulate_sla(uuid,insurance_type,complaint_priority,timestamptz),
    app_security.test_business_hours(jsonb,integer,timestamptz,uuid,insurance_type)
    TO app_runtime;
GRANT EXECUTE ON FUNCTION
    app_security.process_sla_warnings(integer) TO app_worker;
