-- Revision 037: the request archive, and the automation that keeps it fed.
--
-- Revision 036 turned complaints into requests and opened the three intake
-- channels. It left two things the brokerage actually runs its day on:
--
--   1. The archive. Every request a company's employees ever raised has to be
--      retrievable in one place, long after it is closed: searchable by
--      employee, mobile, policy or card, filtered by status, source and date,
--      and openable down to the individual message, attachment and phone call.
--      The portal currently shows requests nested under each employee, which
--      answers "what did Karim ask for" but not "show me everything from last
--      March", and it has no paging, so a company with real volume cannot use
--      it.
--
--   2. The automation. Three things that a human should never have to
--      remember:
--        - acknowledging an inbound request so the sender gets their ticket
--          number back on the channel they wrote in on;
--        - warning the brokerage before a corporate contract reaches its
--          renewal date;
--        - closing requests that were answered and then went quiet.
--      Each one must be exactly-once: an acknowledgement sent twice is worse
--      than one sent late, and a renewal reminder that re-fires every cron
--      tick is noise that gets the whole feature muted.
--
-- The exactly-once property is a single unique index on an automation ledger
-- rather than a flag on each subject row. One ledger means one place to audit
-- what the platform did on its own, which is what an archive is for.
--
-- Everything is expand-only: one new table, new functions, no existing column
-- dropped or rewritten, so this is safe to apply while the API is serving.

-- ===========================================================================
-- 1. The automation ledger
--
-- One row per (kind, subject, key) that the platform acted on by itself.
-- `subject_key` is what makes an action repeatable on a schedule without
-- repeating itself: for a renewal reminder it is the renewal date, so moving
-- a contract's renewal date legitimately re-arms the reminder, while the cron
-- re-running sixty times an hour does not.
-- ===========================================================================

CREATE TABLE public.automation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL,
    subject_id uuid NOT NULL,
    subject_key text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'done',
    channel text,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_automation_kind CHECK (kind IN (
        'request_acknowledgement', 'contract_renewal_notice', 'request_auto_close')),
    CONSTRAINT ck_automation_status CHECK (status IN ('done', 'failed', 'skipped'))
);

COMMENT ON TABLE public.automation_runs IS
    'Ledger of actions the platform took without a human: acknowledgements, '
    'renewal reminders, auto-closures. The unique index is the exactly-once guard.';

CREATE UNIQUE INDEX uq_automation_run
    ON public.automation_runs (kind, subject_id, subject_key);
CREATE INDEX ix_automation_runs_recent
    ON public.automation_runs (created_at DESC);

ALTER TABLE public.automation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.automation_runs FORCE ROW LEVEL SECURITY;

-- Operational telemetry, not customer data: any staff member who may read the
-- request queue may read what the platform did to it. Writes only ever happen
-- through the SECURITY DEFINER helpers below.
CREATE POLICY automation_runs_read ON public.automation_runs
    FOR SELECT USING (app_security.has_permission('complaints.view'));

GRANT SELECT ON public.automation_runs TO app_runtime;

-- ===========================================================================
-- 2. Acknowledgement: which inbound requests still owe the sender a reply
--
-- Only requests that arrived on a channel we can answer on, only those that
-- carry a usable destination, and only recent ones. The age window matters on
-- the first deploy: without it, enabling this feature would blast an
-- acknowledgement at every historical request in the table.
-- ===========================================================================

CREATE FUNCTION app_security.pending_request_acknowledgements(
    p_limit integer DEFAULT 50,
    p_max_age_hours integer DEFAULT 24
)
RETURNS TABLE (
    complaint_id uuid,
    complaint_created_at timestamptz,
    ticket_no text,
    company_id uuid,
    channel text,
    destination text,
    customer_name text,
    subject text
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'automation read forbidden' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT c.id,
           c.created_at,
           c.ticket_no,
           c.company_id,
           c.intake_source,
           CASE WHEN c.intake_source = 'email' THEN cu.email ELSE cu.mobile END,
           cu.full_name,
           c.subject
    FROM public.complaints c
    JOIN public.customers cu ON cu.id = c.customer_id
    WHERE c.intake_source IN ('whatsapp', 'email', 'sms', 'phone')
      AND c.created_at >= now() - make_interval(hours => GREATEST(COALESCE(p_max_age_hours, 24), 1))
      AND COALESCE(
            CASE WHEN c.intake_source = 'email' THEN cu.email ELSE cu.mobile END, '') <> ''
      AND NOT EXISTS (
            SELECT 1 FROM public.automation_runs a
            WHERE a.kind = 'request_acknowledgement'
              AND a.subject_id = c.id
              AND a.subject_key = '')
    ORDER BY c.created_at
    LIMIT GREATEST(COALESCE(p_limit, 50), 1);
END;
$function$;

-- ===========================================================================
-- 3. Acknowledgement: record that we answered (or could not)
--
-- Returns true only when this call is the one that claimed the slot, so a
-- caller can send the message *after* winning the insert and never send twice
-- even if two workers poll the same request at the same moment.
-- ===========================================================================

CREATE FUNCTION app_security.acknowledge_request(
    p_complaint_id uuid,
    p_created_at timestamptz,
    p_channel text
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_claimed boolean := false;
    v_customer uuid;
    v_ticket text;
    v_status text;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'automation write forbidden' USING ERRCODE = '42501';
    END IF;

    -- Claim first. If another worker already holds the slot this returns no
    -- rows and we enqueue nothing, so the customer is never messaged twice.
    INSERT INTO public.automation_runs(kind, subject_id, subject_key, status, channel)
    VALUES ('request_acknowledgement', p_complaint_id, '', 'done',
            left(COALESCE(p_channel, ''), 40))
    ON CONFLICT (kind, subject_id, subject_key) DO NOTHING;
    GET DIAGNOSTICS v_claimed = ROW_COUNT;
    IF NOT v_claimed THEN
        RETURN false;
    END IF;

    SELECT c.customer_id, c.ticket_no, c.status::text
      INTO v_customer, v_ticket, v_status
      FROM public.complaints c
     WHERE c.id = p_complaint_id AND c.created_at = p_created_at;
    IF v_customer IS NULL THEN
        RAISE EXCEPTION 'request not found' USING ERRCODE = 'P0002';
    END IF;

    -- Same two writes the staff-initiated notice path performs in revision
    -- 006: the durable outbox the delivery cron drains, and the in-app
    -- notification. Reusing them means acknowledgements inherit retries,
    -- per-customer channel preferences and suppression for free.
    INSERT INTO public.notification_outbox
        (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
    VALUES (p_complaint_id, p_created_at, v_customer, 'created',
            jsonb_build_object('ticket_no', v_ticket, 'status', v_status,
                               'intake_source', p_channel, 'automated', true));
    INSERT INTO public.notifications
        (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
    VALUES (v_customer, p_complaint_id, p_created_at, 'created',
            jsonb_build_object('ticket_no', v_ticket, 'status', v_status,
                               'intake_source', p_channel, 'automated', true));
    INSERT INTO public.complaint_events(
        complaint_id, complaint_created_at, event_type, metadata, visibility)
    VALUES (p_complaint_id, p_created_at, 'auto_acknowledged',
            jsonb_build_object('channel', p_channel), 'public');
    RETURN true;
END;
$function$;

-- Recording an acknowledgement that could not be sent, so the poller stops
-- retrying a request whose customer has no reachable address at all.
CREATE FUNCTION app_security.mark_request_acknowledged(
    p_complaint_id uuid,
    p_created_at timestamptz,
    p_channel text,
    p_status text DEFAULT 'skipped',
    p_detail jsonb DEFAULT '{}'::jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_claimed boolean := false;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'automation write forbidden' USING ERRCODE = '42501';
    END IF;
    IF COALESCE(p_status, '') NOT IN ('done', 'failed', 'skipped') THEN
        RAISE EXCEPTION 'unsupported automation status' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.automation_runs(kind, subject_id, subject_key, status, channel, detail)
    VALUES ('request_acknowledgement', p_complaint_id, '', p_status,
            left(COALESCE(p_channel, ''), 40), COALESCE(p_detail, '{}'::jsonb))
    ON CONFLICT (kind, subject_id, subject_key) DO NOTHING;
    GET DIAGNOSTICS v_claimed = ROW_COUNT;
    RETURN v_claimed;
END;
$function$;

-- ===========================================================================
-- 4. Contract renewals that are coming up
--
-- `subject_key` is the renewal date, so a contract renewed for another year
-- re-arms the reminder the moment someone moves the date forward.
-- ===========================================================================

CREATE FUNCTION app_security.pending_contract_renewals(
    p_within_days integer DEFAULT 30,
    p_limit integer DEFAULT 100
)
RETURNS TABLE (
    contract_id uuid,
    corporate_client_id uuid,
    client_name text,
    contract_no text,
    renews_on date,
    days_to_renewal integer,
    contract_value numeric,
    currency text,
    payment_frequency text,
    headcount integer
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('corporate.view')
    ) THEN
        RAISE EXCEPTION 'renewal read forbidden' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    SELECT k.id,
           k.corporate_client_id,
           cc.name_ar,
           k.contract_no,
           k.renews_on,
           (k.renews_on - CURRENT_DATE)::integer,
           k.contract_value,
           k.currency::text,
           k.payment_frequency::text,
           k.headcount
    FROM public.corporate_contracts k
    JOIN public.corporate_clients cc ON cc.id = k.corporate_client_id
    WHERE k.status = 'active'
      AND k.renews_on IS NOT NULL
      AND k.renews_on >= CURRENT_DATE
      AND k.renews_on <= CURRENT_DATE + GREATEST(COALESCE(p_within_days, 30), 1)
      AND NOT EXISTS (
            SELECT 1 FROM public.automation_runs a
            WHERE a.kind = 'contract_renewal_notice'
              AND a.subject_id = k.id
              AND a.subject_key = k.renews_on::text)
    ORDER BY k.renews_on
    LIMIT GREATEST(COALESCE(p_limit, 100), 1);
END;
$function$;

CREATE FUNCTION app_security.mark_contract_renewal_notified(
    p_contract_id uuid,
    p_renews_on date,
    p_status text DEFAULT 'done',
    p_detail jsonb DEFAULT '{}'::jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_claimed boolean := false;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('corporate.manage')
    ) THEN
        RAISE EXCEPTION 'renewal write forbidden' USING ERRCODE = '42501';
    END IF;
    IF COALESCE(p_status, '') NOT IN ('done', 'failed', 'skipped') THEN
        RAISE EXCEPTION 'unsupported automation status' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.automation_runs(kind, subject_id, subject_key, status, detail)
    VALUES ('contract_renewal_notice', p_contract_id, p_renews_on::text, p_status,
            COALESCE(p_detail, '{}'::jsonb))
    ON CONFLICT (kind, subject_id, subject_key) DO NOTHING;
    GET DIAGNOSTICS v_claimed = ROW_COUNT;
    RETURN v_claimed;
END;
$function$;

-- ===========================================================================
-- 5. Auto-close: requests that were answered and then went quiet
--
-- A broker's queue silts up with requests that were resolved in substance but
-- never clicked closed. A request qualifies only if staff actually replied
-- (an outbound message exists), the customer has not written since, and the
-- quiet period has passed.
-- ===========================================================================

CREATE FUNCTION app_security.pending_request_auto_close(
    p_quiet_days integer DEFAULT 14,
    p_limit integer DEFAULT 100
)
RETURNS TABLE (
    complaint_id uuid,
    complaint_created_at timestamptz,
    ticket_no text,
    status text,
    last_outbound_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'automation read forbidden' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY
    WITH quiet AS (
        SELECT c.id,
               c.created_at,
               c.ticket_no,
               c.status::text AS status,
               -- Only a *public* outbound message is an answer to the
               -- customer. An internal note is the brokerage talking to
               -- itself, and must not start the quiet clock.
               max(m.created_at) FILTER (
                   WHERE m.direction = 'outbound' AND m.visibility = 'public') AS last_outbound,
               -- Any inbound message means the customer is still waiting,
               -- whatever its visibility.
               max(m.created_at) FILTER (WHERE m.direction = 'inbound') AS last_inbound
        FROM public.complaints c
        JOIN public.complaint_messages m
          ON m.complaint_id = c.id AND m.complaint_created_at = c.created_at
        WHERE c.status IN ('new', 'assigned', 'in_review')
        GROUP BY c.id, c.created_at, c.ticket_no, c.status
    )
    SELECT q.id, q.created_at, q.ticket_no, q.status, q.last_outbound
    FROM quiet q
    WHERE q.last_outbound IS NOT NULL
      AND q.last_outbound < now() - make_interval(days => GREATEST(COALESCE(p_quiet_days, 14), 1))
      AND (q.last_inbound IS NULL OR q.last_inbound <= q.last_outbound)
      AND NOT EXISTS (
            SELECT 1 FROM public.automation_runs a
            WHERE a.kind = 'request_auto_close'
              AND a.subject_id = q.id
              AND a.subject_key = '')
    ORDER BY q.last_outbound
    LIMIT GREATEST(COALESCE(p_limit, 100), 1);
END;
$function$;

CREATE FUNCTION app_security.auto_close_request(
    p_complaint_id uuid,
    p_created_at timestamptz,
    p_quiet_days integer DEFAULT 14
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_claimed boolean := false;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'automation write forbidden' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.automation_runs(kind, subject_id, subject_key, status, detail)
    VALUES ('request_auto_close', p_complaint_id, '', 'done',
            jsonb_build_object('quiet_days', p_quiet_days))
    ON CONFLICT (kind, subject_id, subject_key) DO NOTHING;
    GET DIAGNOSTICS v_claimed = ROW_COUNT;
    IF NOT v_claimed THEN
        RETURN false;
    END IF;

    -- Re-check the status inside the writing transaction: a human may have
    -- moved the request between the poll and this call.
    UPDATE public.complaints
       SET status = 'resolved', resolved_at = now(), updated_at = now()
     WHERE id = p_complaint_id
       AND created_at = p_created_at
       AND status IN ('new', 'assigned', 'in_review');
    GET DIAGNOSTICS v_claimed = ROW_COUNT;
    IF v_claimed THEN
        INSERT INTO public.complaint_events(
            complaint_id, complaint_created_at, event_type, metadata)
        VALUES (p_complaint_id, p_created_at, 'auto_closed',
                jsonb_build_object('reason', 'no_reply', 'quiet_days', p_quiet_days));
    END IF;
    RETURN v_claimed;
END;
$function$;

-- ===========================================================================
-- 6. The archive: every request this company ever raised
--
-- One function, because the portal needs the page, the total and the counters
-- to agree with each other; computing them in three round trips lets them
-- disagree under concurrent intake. Filters are all optional and compose.
--
-- `p_search` is deliberately generous — a user pastes a ticket number, a
-- mobile, a card number or half a name into one box. The digits branch is
-- guarded with NULLIF so a non-numeric term does not collapse to LIKE '%%'
-- and match the entire company.
-- ===========================================================================

CREATE FUNCTION app_security.corporate_request_archive(
    p_search text DEFAULT NULL,
    p_status text DEFAULT NULL,
    p_source text DEFAULT NULL,
    p_from date DEFAULT NULL,
    p_to date DEFAULT NULL,
    p_limit integer DEFAULT 25,
    p_offset integer DEFAULT 0
) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    WITH params AS (
        SELECT app_security.corporate_client_id() AS client,
               btrim(COALESCE(p_search, '')) AS raw,
               NULLIF(regexp_replace(COALESCE(p_search, ''), '\D', '', 'g'), '') AS digits,
               LEAST(GREATEST(COALESCE(p_limit, 25), 1), 100) AS lim,
               GREATEST(COALESCE(p_offset, 0), 0) AS off
    ),
    matched AS (
        SELECT c.id, c.created_at, c.updated_at, c.resolved_at, c.ticket_no, c.subject,
               c.status, c.priority, c.intake_source, c.channel,
               c.policy_number, c.card_number,
               cu.id AS employee_id, cu.full_name AS employee_name,
               cu.employee_code, cu.mobile AS employee_mobile
        FROM public.complaints c
        LEFT JOIN public.customers cu ON cu.id = c.customer_id
        CROSS JOIN params p
        WHERE p.client IS NOT NULL
          AND c.corporate_client_id = p.client
          AND (p_status IS NULL OR btrim(p_status) = '' OR c.status::text = btrim(p_status))
          AND (p_source IS NULL OR btrim(p_source) = '' OR c.intake_source = btrim(p_source))
          AND (p_from IS NULL OR c.created_at >= p_from::timestamptz)
          AND (p_to IS NULL OR c.created_at < (p_to + 1)::timestamptz)
          AND (
                p.raw = ''
                OR c.ticket_no ILIKE '%' || p.raw || '%'
                OR c.subject ILIKE '%' || p.raw || '%'
                OR c.policy_number ILIKE '%' || p.raw || '%'
                OR c.card_number ILIKE '%' || p.raw || '%'
                OR cu.full_name ILIKE '%' || p.raw || '%'
                OR (p.digits IS NOT NULL AND cu.mobile LIKE '%' || p.digits || '%')
              )
    ),
    page AS (
        SELECT m.*
        FROM matched m
        ORDER BY m.created_at DESC
        LIMIT (SELECT lim FROM params) OFFSET (SELECT off FROM params)
    )
    SELECT jsonb_build_object(
        'items', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'id', g.id,
                       'created_at', g.created_at,
                       'updated_at', g.updated_at,
                       'resolved_at', g.resolved_at,
                       'ticket_no', g.ticket_no,
                       'subject', g.subject,
                       'status', g.status::text,
                       'priority', g.priority::text,
                       'intake_source', g.intake_source,
                       'channel', g.channel::text,
                       'policy_number', g.policy_number,
                       'card_number', g.card_number,
                       'employee_id', g.employee_id,
                       'employee_name', g.employee_name,
                       'employee_code', g.employee_code,
                       'employee_mobile', g.employee_mobile,
                       'messages_count', (
                           SELECT count(*) FROM public.complaint_messages x
                           WHERE x.complaint_id = g.id
                             AND x.complaint_created_at = g.created_at),
                       'attachments_count', (
                           SELECT count(*) FROM public.attachments x
                           WHERE x.complaint_id = g.id
                             AND x.complaint_created_at = g.created_at),
                       'calls_count', (
                           SELECT count(*) FROM public.call_logs x
                           WHERE x.complaint_id = g.id
                             AND x.complaint_created_at = g.created_at))
                   ORDER BY g.created_at DESC)
            FROM page g), '[]'::jsonb),
        'total', (SELECT count(*) FROM matched),
        -- Counters describe the filtered set, not the page: a company
        -- filtering to "WhatsApp, March" wants the breakdown of that slice.
        'summary', jsonb_build_object(
            'by_status', COALESCE((
                SELECT jsonb_object_agg(s, n)
                FROM (SELECT status::text AS s, count(*) AS n FROM matched GROUP BY 1) q),
                '{}'::jsonb),
            'by_source', COALESCE((
                SELECT jsonb_object_agg(s, n)
                FROM (SELECT intake_source AS s, count(*) AS n FROM matched GROUP BY 1) q),
                '{}'::jsonb),
            'open', (SELECT count(*) FROM matched WHERE status <> 'resolved'),
            'first_at', (SELECT min(created_at) FROM matched),
            'last_at', (SELECT max(created_at) FROM matched)),
        'limit', (SELECT lim FROM params),
        'offset', (SELECT off FROM params))
$function$;

-- ===========================================================================
-- 7. The archive, drilled into one request
--
-- Messages, attachment metadata and call metadata in one timeline. Deliberate
-- omissions: no message body ciphertext keys, no attachment bytes and no call
-- recordings. The company sees that a ten-minute call happened on the 3rd and
-- that a 2 MB PDF came with it; retrieving either is a separate, separately
-- audited download.
-- ===========================================================================

CREATE FUNCTION app_security.corporate_request_timeline(p_request_id uuid)
RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_client uuid := app_security.corporate_client_id();
    v_row public.complaints%ROWTYPE;
    v_customer public.customers%ROWTYPE;
BEGIN
    IF v_client IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_row FROM public.complaints
     WHERE id = p_request_id AND corporate_client_id = v_client
     LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT * INTO v_customer FROM public.customers WHERE id = v_row.customer_id;

    RETURN jsonb_build_object(
        'id', v_row.id,
        'ticket_no', v_row.ticket_no,
        'subject', v_row.subject,
        'status', v_row.status::text,
        'priority', v_row.priority::text,
        'intake_source', v_row.intake_source,
        'channel', v_row.channel::text,
        'policy_number', v_row.policy_number,
        'card_number', v_row.card_number,
        'created_at', v_row.created_at,
        'updated_at', v_row.updated_at,
        'resolved_at', v_row.resolved_at,
        'employee', CASE WHEN v_customer.id IS NULL THEN NULL ELSE jsonb_build_object(
            'id', v_customer.id, 'full_name', v_customer.full_name,
            'mobile', v_customer.mobile, 'email', v_customer.email,
            'employee_code', v_customer.employee_code) END,
        -- `visibility = 'public'` throughout: internal staff notes, internal
        -- attachments and internal events are the brokerage's working notes
        -- about the company, and the company is not the audience for them.
        'messages', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'id', m.id, 'direction', m.direction::text,
                       'channel', m.channel::text, 'created_at', m.created_at,
                       'body', m.body,
                       'delivery_status', m.delivery_status)
                   ORDER BY m.created_at)
            FROM public.complaint_messages m
            WHERE m.complaint_id = v_row.id
              AND m.complaint_created_at = v_row.created_at
              AND m.visibility = 'public'), '[]'::jsonb),
        'attachments', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'id', t.id, 'filename', t.original_filename,
                       'mime_type', t.mime_type, 'size_bytes', t.size_bytes,
                       'scan_status', t.scan_status::text,
                       'created_at', t.created_at)
                   ORDER BY t.created_at)
            FROM public.attachments t
            WHERE t.complaint_id = v_row.id
              AND t.complaint_created_at = v_row.created_at
              AND t.visibility = 'public'), '[]'::jsonb),
        -- Calls are listed as metadata only. That a ninety-second call
        -- happened on the 3rd is part of the company's record; the recording
        -- and the transcript are not handed out here.
        'calls', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'id', l.id, 'direction', l.direction::text, 'provider', l.provider,
                       'outcome', l.outcome, 'status', l.status,
                       'started_at', l.started_at, 'ended_at', l.ended_at,
                       'duration_seconds', CASE
                           WHEN l.started_at IS NULL OR l.ended_at IS NULL THEN NULL
                           ELSE GREATEST(
                               EXTRACT(EPOCH FROM (l.ended_at - l.started_at))::integer, 0) END,
                       'recorded', (l.recording_content IS NOT NULL
                                    OR l.recording_storage_key IS NOT NULL),
                       'consented', l.consent_at IS NOT NULL)
                   ORDER BY l.started_at)
            FROM public.call_logs l
            WHERE l.complaint_id = v_row.id
              AND l.complaint_created_at = v_row.created_at), '[]'::jsonb),
        'events', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                       'event_type', e.event_type, 'occurred_at', e.occurred_at,
                       'from_status', e.from_status::text, 'to_status', e.to_status::text)
                   ORDER BY e.occurred_at)
            FROM public.complaint_events e
            WHERE e.complaint_id = v_row.id
              AND e.complaint_created_at = v_row.created_at
              AND e.visibility = 'public'), '[]'::jsonb));
END;
$function$;

-- ===========================================================================
-- 8. Grants
--
-- Read paths to the web role, automation write paths to the worker only.
-- Granted per function: a blanket GRANT ON ALL FUNCTIONS at this point in the
-- chain would hand app_runtime the worker's intake entry points.
-- ===========================================================================

REVOKE ALL ON FUNCTION app_security.pending_request_acknowledgements(integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.acknowledge_request(uuid, timestamptz, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.mark_request_acknowledged(uuid, timestamptz, text, text, jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.pending_contract_renewals(integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.mark_contract_renewal_notified(uuid, date, text, jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.pending_request_auto_close(integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.auto_close_request(uuid, timestamptz, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.corporate_request_archive(
    text, text, text, date, date, integer, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.corporate_request_timeline(uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION app_security.pending_request_acknowledgements(integer, integer)
    TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.acknowledge_request(uuid, timestamptz, text) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.mark_request_acknowledged(uuid, timestamptz, text, text, jsonb)
    TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.pending_contract_renewals(integer, integer) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.mark_contract_renewal_notified(uuid, date, text, jsonb)
    TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.pending_request_auto_close(integer, integer) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.auto_close_request(uuid, timestamptz, integer) TO app_worker;

-- The portal reads the archive; it never drives automation.
GRANT EXECUTE ON FUNCTION app_security.corporate_request_archive(
    text, text, text, date, date, integer, integer) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_request_timeline(uuid) TO app_runtime;

-- Staff screens surface upcoming renewals through the same helper the worker
-- polls, so the number on the dashboard is the number that will be actioned.
GRANT EXECUTE ON FUNCTION app_security.pending_contract_renewals(integer, integer) TO app_runtime;

-- ===========================================================================
-- 9. Telephone intake: let a call open a request
--
-- Revision 036 made `phone` a first-class intake source and added
-- `record_request_call`, but nothing could reach it: the voice webhook
-- rejected any call that did not quote a REQ- number, which is nearly every
-- call. People ring a broker to *start* something.
--
-- A ticket-less call now enqueues `inbound_call`, which the worker routes
-- through the same intake service WhatsApp and e-mail use.
--
-- Widening a CHECK is backwards compatible — the running version writes a
-- subset of the new set — and is done in the three-step form so the table is
-- never scanned under an exclusive lock: drop, re-add NOT VALID, validate.
-- ===========================================================================

ALTER TABLE public.integration_jobs DROP CONSTRAINT ck_integration_job_kind;
ALTER TABLE public.integration_jobs ADD CONSTRAINT ck_integration_job_kind CHECK (
    job_kind = ANY (ARRAY[
        'send_email', 'send_whatsapp', 'send_sms', 'ai_analyze', 'voice_followup',
        'webhook_send', 'inbound_email', 'inbound_whatsapp', 'inbound_sms',
        'whatsapp_status', 'email_status', 'sms_status',
        'inbound_voice', 'inbound_call', 'voice_status'])) NOT VALID;
ALTER TABLE public.integration_jobs VALIDATE CONSTRAINT ck_integration_job_kind;

-- `inbound_call` matches the `inbound_%` arm of ck_integration_job_sender, so
-- it is already allowed to have no staff sender. CREATE OR REPLACE keeps the
-- existing ACLs; DROP + CREATE would silently lose them.
CREATE OR REPLACE FUNCTION app_security.enqueue_inbound_job(
    p_company uuid, p_provider text, p_job_kind text, p_ciphertext bytea,
    p_idempotency bytea, p_payload_digest bytea)
RETURNS TABLE (job_id uuid, created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_created boolean := false;
BEGIN
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'webhook'
       OR p_job_kind NOT IN ('inbound_email','inbound_whatsapp','whatsapp_status',
                             'email_status','inbound_voice','inbound_call','voice_status',
                             'inbound_sms','sms_status')
       OR p_provider NOT IN ('email_webhook','whatsapp_cloud','mock_voice','twilio','vonage',
                             'imap','twilio_sms')
       OR NOT EXISTS (SELECT 1 FROM public.insurance_companies c WHERE c.id = p_company)
       OR octet_length(p_ciphertext) < 28 OR octet_length(p_idempotency) <> 32
       OR octet_length(p_payload_digest) <> 32 THEN
        RAISE EXCEPTION 'inbound event invalid' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.integration_jobs(job_kind, provider, company_id, payload_ciphertext,
        idempotency_digest, payload_digest)
    VALUES (p_job_kind, p_provider, p_company, p_ciphertext, p_idempotency, p_payload_digest)
    ON CONFLICT (idempotency_digest) DO NOTHING
    RETURNING id INTO v_id;
    v_created := FOUND;
    IF NOT v_created THEN
        SELECT j.id INTO v_id FROM public.integration_jobs j
         WHERE j.idempotency_digest = p_idempotency;
    END IF;
    RETURN QUERY SELECT v_id, v_created;
END;
$function$;
