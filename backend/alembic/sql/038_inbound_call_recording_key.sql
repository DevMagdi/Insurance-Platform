-- Revision 038: give an inbound call recording a key its reader can rebuild.
--
-- A recording and its transcript are encrypted with the call's identifier
-- woven into the authenticated associated data, so a blob cannot be lifted
-- out of one row and replayed in another. That binding only works while the
-- writer and the reader agree on which identifier it is.
--
-- They did not agree. `record_request_call` minted the primary key itself
-- with `gen_random_uuid()` and only returned it after the row was written, so
-- the worker -- which has to encrypt *before* it can insert -- bound inbound
-- audio to a derived identifier of its own: uuid5 over the company and the
-- provider's call id. Every reader rebuilds the context from `call_logs.id`
-- instead: the staff download route, the request timeline and the key
-- rotation job. The two values were never the same one, so a recording
-- arriving from a phone call was stored intact and then could not be
-- decrypted by anything, ever. Outbound calls were unaffected, because there
-- the row exists first and the worker already encrypts against its real id.
--
-- The fix is to let the caller name the row. This overload accepts the
-- identifier the ciphertext was bound to and uses it as the primary key, so
-- the reader's `call_logs.id` is the exact value the writer sealed in. A
-- caller-supplied, deterministic key also makes the write idempotent: a
-- redelivered provider callback now collides on the primary key instead of
-- logging the same conversation twice, and the timeline entry is only
-- appended when a row was actually inserted.
--
-- The original ten-argument signature stays and delegates with a NULL
-- identifier, so a pod still serving the previous release keeps working
-- through a rolling deploy. Retiring it is a contract step for a later
-- revision. Recordings written before this revision stay unreadable until
-- `python -m app.scripts.repair_call_recording_aad` re-seals them, which it
-- can do because the old context is recomputable from the same two columns.

CREATE FUNCTION app_security.record_request_call(
    p_complaint_id uuid, p_created_at timestamptz,
    p_direction public.message_direction, p_provider text,
    p_provider_call_id text, p_recording bytea, p_transcript bytea,
    p_outcome text, p_consent boolean, p_duration_seconds integer,
    p_call_id uuid
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_id uuid := COALESCE(p_call_id, gen_random_uuid());
    v_written integer;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'call intake forbidden' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.complaints
                   WHERE id = p_complaint_id AND created_at = p_created_at) THEN
        RAISE EXCEPTION 'request not found' USING ERRCODE = 'P0002';
    END IF;
    -- A recording is personal data: it is only retained with recorded consent.
    IF p_recording IS NOT NULL AND NOT COALESCE(p_consent, false) THEN
        RAISE EXCEPTION 'call recording requires consent' USING ERRCODE = '42501';
    END IF;
    IF p_recording IS NOT NULL AND octet_length(p_recording) < 28 THEN
        RAISE EXCEPTION 'call recording must be encrypted' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.call_logs(
        id, complaint_id, complaint_created_at, direction, provider,
        provider_call_id, recording_content, transcript_ciphertext, outcome,
        consent_at, status, started_at, ended_at)
    VALUES (v_id, p_complaint_id, p_created_at, p_direction, left(p_provider, 60),
        left(p_provider_call_id, 256), p_recording, p_transcript,
        left(COALESCE(p_outcome, 'completed'), 80),
        CASE WHEN COALESCE(p_consent, false) THEN now() ELSE NULL END,
        'completed', now() - make_interval(secs => GREATEST(COALESCE(p_duration_seconds, 0), 0)),
        now())
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS v_written = ROW_COUNT;
    -- A redelivered callback must not append the call to the timeline twice.
    IF v_written = 0 THEN
        RETURN v_id;
    END IF;
    INSERT INTO public.complaint_events(
        complaint_id, complaint_created_at, event_type, metadata)
    VALUES (p_complaint_id, p_created_at, 'inbound_voice_call',
        jsonb_build_object('call_log_id', v_id, 'recorded', p_recording IS NOT NULL,
                           'consent', COALESCE(p_consent, false),
                           'duration_seconds', GREATEST(COALESCE(p_duration_seconds, 0), 0)));
    RETURN v_id;
END;
$function$;

-- The previous signature now forwards to the one above. Keeping it means a
-- release still running the old worker writes rows exactly as it did before.
CREATE OR REPLACE FUNCTION app_security.record_request_call(
    p_complaint_id uuid, p_created_at timestamptz,
    p_direction public.message_direction, p_provider text,
    p_provider_call_id text, p_recording bytea, p_transcript bytea,
    p_outcome text, p_consent boolean, p_duration_seconds integer
) RETURNS uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.record_request_call(
        p_complaint_id, p_created_at, p_direction, p_provider,
        p_provider_call_id, p_recording, p_transcript, p_outcome, p_consent,
        p_duration_seconds, NULL::uuid)
$function$;

REVOKE ALL ON FUNCTION app_security.record_request_call(
    uuid, timestamptz, public.message_direction, text, text, bytea, bytea, text,
    boolean, integer, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.record_request_call(
    uuid, timestamptz, public.message_direction, text, text, bytea, bytea, text,
    boolean, integer, uuid) TO app_worker;
