-- Keep the denormalised complaints.csat_score in step with csat_responses.
-- Analytics rollups, exports and the staff ticket view all read the column, so
-- a submitted score that only lived in csat_responses made CSAT look unanswered
-- everywhere outside the customer's own screen.
UPDATE public.complaints AS c
SET csat_score = r.score
FROM public.csat_responses AS r
WHERE r.complaint_id = c.id
  AND r.complaint_created_at = c.created_at
  AND c.csat_score IS NULL;

CREATE OR REPLACE FUNCTION app_security.submit_csat(p_id uuid, p_created timestamptz,
    p_score smallint, p_comment text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_complaint complaints%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'customer'
       OR NOT app_security.can_read_complaint_id(p_id, p_created)
       OR p_score NOT BETWEEN 1 AND 5 OR length(coalesce(p_comment, '')) > 1000 THEN
        RETURN false;
    END IF;
    SELECT * INTO v_complaint FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR v_complaint.resolved_at IS NULL
       OR v_complaint.resolved_at < now() - interval '30 days'
       OR EXISTS (SELECT 1 FROM public.csat_responses
          WHERE complaint_id = p_id AND complaint_created_at = p_created
            AND customer_id = app_security.actor_id()) THEN
        RETURN false;
    END IF;
    INSERT INTO public.csat_responses
        (complaint_id, complaint_created_at, customer_id, score, comment)
        VALUES (p_id, p_created, app_security.actor_id(), p_score,
            nullif(btrim(p_comment), ''));
    UPDATE public.complaints
        SET csat_score = p_score, updated_at = now()
        WHERE id = p_id AND created_at = p_created;
    INSERT INTO public.audit_logs (action, resource_type, resource_id,
        complaint_id, complaint_created_at)
        VALUES ('csat', 'complaint', p_id, p_id, p_created);
    RETURN true;
END;
$function$;
