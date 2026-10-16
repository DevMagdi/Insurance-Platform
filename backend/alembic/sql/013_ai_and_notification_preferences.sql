-- AI insight writes/reviews and customer notification-preference delivery.
-- AI payloads remain encrypted application data; RLS only permits staff who
-- can already read the parent complaint and hold the complaint-edit floor.

CREATE POLICY ai_insights_insert ON public.ai_insights FOR INSERT TO app_runtime
    WITH CHECK (app_security.actor_scope() = 'staff'
        AND app_security.has_permission('complaints.edit')
        AND app_security.can_read_complaint_id(complaint_id, complaint_created_at));

CREATE POLICY ai_insights_review ON public.ai_insights FOR UPDATE TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND app_security.has_permission('complaints.edit')
        AND app_security.can_read_complaint_id(complaint_id, complaint_created_at))
    WITH CHECK (app_security.actor_scope() = 'staff'
        AND app_security.has_permission('complaints.edit')
        AND app_security.can_read_complaint_id(complaint_id, complaint_created_at));

GRANT INSERT ON public.ai_insights TO app_runtime;
GRANT UPDATE (review_status, reviewed_by_staff_id) ON public.ai_insights TO app_runtime;

ALTER TABLE public.notification_outbox
    DROP CONSTRAINT notification_outbox_state_check;
ALTER TABLE public.notification_outbox
    ADD CONSTRAINT notification_outbox_state_check
    CHECK (state IN ('queued', 'processing', 'sent', 'pending_provider', 'failed', 'suppressed'));

DROP FUNCTION IF EXISTS app_security.claim_notices(integer);
CREATE FUNCTION app_security.claim_notices(p_limit integer DEFAULT 20)
RETURNS TABLE (notice_id uuid, kind text, payload jsonb, mobile text, email text,
    notification_preferences jsonb)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    RETURN QUERY WITH claim AS (
        SELECT o.id FROM public.notification_outbox o
        WHERE o.state IN ('queued', 'processing') AND o.attempts < 10
          AND o.next_attempt_at <= now()
        ORDER BY o.next_attempt_at, o.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    ), updated AS (
        UPDATE public.notification_outbox o SET state = 'processing',
            attempts = o.attempts + 1, next_attempt_at = now() + interval '5 minutes'
            FROM claim WHERE o.id = claim.id
        RETURNING o.id, o.kind, o.payload, o.recipient_customer_id
    ) SELECT u.id, u.kind, u.payload, c.mobile, c.email,
        COALESCE(c.notification_preferences,
            '{"email": true, "whatsapp": true, "sms": true}'::jsonb)
        FROM updated u JOIN public.customers c ON c.id = u.recipient_customer_id;
END;
$function$;

CREATE OR REPLACE FUNCTION app_security.finish_notice(p_id uuid, p_state text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_state NOT IN ('sent', 'pending_provider', 'failed', 'retry', 'suppressed')
       THEN RETURN false; END IF;
    UPDATE public.notification_outbox SET
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

REVOKE ALL ON FUNCTION app_security.claim_notices(integer),
    app_security.finish_notice(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.claim_notices(integer),
    app_security.finish_notice(uuid, text) TO app_worker;
