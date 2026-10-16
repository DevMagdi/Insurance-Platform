-- PostgreSQL is the only store for private binaries (revision 24).
--
-- Attachments, generated export artifacts and call recordings used to live in an
-- S3-compatible object store. They are now columns on the rows that own them:
-- the transaction that records metadata and the one that stores the bytes are the
-- same, so there is no orphan object to reconcile, no signed URL to leak and no
-- second stateful service to patch. Bytes are written by the application encrypted
-- with APP_ENCRYPTION_KEY ("KR" header + key id + nonce + GCM tag = 31 bytes of
-- overhead), so a dump without the deployment secret is not readable.
--
-- Legacy locator columns stay in place (nullable) so a rolling deployment can run
-- old and new pods side by side; they are no longer written or read and a later
-- contract revision can drop them.

-- 1. Attachment content ------------------------------------------------------------------
ALTER TABLE public.attachments
    ADD COLUMN content bytea;
ALTER TABLE public.attachments
    ADD CONSTRAINT ck_attachment_content_size
    CHECK (content IS NULL OR octet_length(content) BETWEEN 31 AND 10485791);
ALTER TABLE public.attachments ALTER COLUMN storage_key DROP NOT NULL;
-- The reservation row no longer names an object: the bytes are written straight
-- onto the attachment row, so the legacy locator becomes nullable here too.
ALTER TABLE public.attachment_uploads ALTER COLUMN storage_key DROP NOT NULL;
COMMENT ON COLUMN public.attachments.content IS
    'Encrypted attachment bytes (AES-256-GCM, APP_ENCRYPTION_KEY); NULL only for rows created before revision 24.';
COMMENT ON COLUMN public.attachments.storage_key IS
    'Deprecated object-storage locator; nullable and unused since revision 24.';

-- 2. Export artifacts --------------------------------------------------------------------
ALTER TABLE public.export_jobs
    ADD COLUMN content bytea;
ALTER TABLE public.export_jobs
    ADD CONSTRAINT ck_export_content_size
    CHECK (content IS NULL OR octet_length(content) BETWEEN 31 AND 2000031);
COMMENT ON COLUMN public.export_jobs.content IS
    'Encrypted CSV/Excel/PDF artifact (AES-256-GCM) for a short-lived download.';
COMMENT ON COLUMN public.export_jobs.private_storage_key IS
    'Deprecated object-storage locator; unused since revision 24.';

-- 3. Call recordings ---------------------------------------------------------------------
ALTER TABLE public.call_logs
    ADD COLUMN recording_content bytea;
ALTER TABLE public.call_logs
    ADD CONSTRAINT ck_call_recording_size
    CHECK (recording_content IS NULL OR octet_length(recording_content) BETWEEN 31 AND 26214431);
COMMENT ON COLUMN public.call_logs.recording_content IS
    'Encrypted recording bytes (AES-256-GCM, purpose-bound to the call id).';
COMMENT ON COLUMN public.call_logs.recording_storage_key IS
    'Deprecated object-storage locator; unused since revision 24.';

-- 4. Virus scan: hand the worker one bounded row at a time, never a bucket listing --------
DROP FUNCTION app_security.pending_scans(integer);
CREATE FUNCTION app_security.pending_scans(p_limit integer DEFAULT 20)
RETURNS TABLE (upload_id uuid, attachment_id uuid, original_filename text, mime_type text,
               size_bytes bigint, checksum_sha256 text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, a.id, a.original_filename, a.mime_type, a.size_bytes, a.checksum_sha256
    FROM public.attachment_uploads u
    JOIN public.attachments a ON a.upload_id = u.id
    WHERE session_user = 'app_worker' AND app_security.actor_scope() = 'worker'
      AND p_limit BETWEEN 1 AND 100 AND u.status = 'uploaded'
      AND a.scan_status = 'pending' AND a.content IS NOT NULL
    ORDER BY u.finalized_at, u.id LIMIT p_limit
$function$;

CREATE FUNCTION app_security.attachment_scan_content(p_upload uuid, p_attachment uuid)
RETURNS bytea
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_content bytea;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker' THEN
        RETURN NULL;
    END IF;
    SELECT a.content INTO v_content FROM public.attachments a
      JOIN public.attachment_uploads u ON u.id = a.upload_id
      WHERE a.id = p_attachment AND u.id = p_upload AND u.status = 'uploaded'
        AND a.scan_status = 'pending';
    RETURN v_content;
END;
$function$;

-- 5. Outbound attachments are read from the row, not from a bucket key -------------------
DROP FUNCTION app_security.clean_job_attachments(uuid, integer, uuid[]);
CREATE FUNCTION app_security.clean_job_attachments(
    p_job uuid, p_attempt integer, p_attachment_ids uuid[])
RETURNS TABLE (attachment_id uuid, original_filename text, mime_type text, size_bytes bigint,
               checksum_sha256 text, content bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_complaint uuid; v_created timestamptz; v_count integer;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_attachment_ids IS NULL OR cardinality(p_attachment_ids) > 5 THEN RETURN; END IF;
    SELECT j.complaint_id, j.complaint_created_at INTO v_complaint, v_created
       FROM public.integration_jobs j WHERE j.id = p_job AND j.state = 'processing'
         AND j.attempts = p_attempt AND j.job_kind = 'send_email';
    IF v_complaint IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO v_count FROM public.attachments a
      WHERE a.complaint_id = v_complaint AND a.complaint_created_at = v_created
        AND a.scan_status = 'clean' AND a.id = ANY(p_attachment_ids);
    IF v_count <> cardinality(p_attachment_ids) THEN
        RAISE EXCEPTION 'outbound attachment is not clean or not on ticket' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY SELECT a.id, a.original_filename, a.mime_type, a.size_bytes,
        a.checksum_sha256, a.content
      FROM public.attachments a
      WHERE a.complaint_id = v_complaint AND a.complaint_created_at = v_created
        AND a.scan_status = 'clean' AND a.id = ANY(p_attachment_ids)
      ORDER BY array_position(p_attachment_ids, a.id);
END;
$function$;

-- 6. Call recordings are stored on the call log row --------------------------------------
DROP FUNCTION app_security.create_call_job(uuid, integer, text, text, bytea, text);
CREATE FUNCTION app_security.create_call_job(
    p_id uuid, p_attempt integer, p_provider_id text, p_outcome text, p_ciphertext bytea,
    p_recording bytea DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR length(p_outcome) > 80
       OR (p_ciphertext IS NOT NULL AND octet_length(p_ciphertext) < 28)
       OR (p_recording IS NOT NULL
           AND octet_length(p_recording) NOT BETWEEN 31 AND 26214431) THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id = p_id
        AND q.job_kind = 'voice_followup'
        AND q.state = 'processing' AND q.attempts = p_attempt FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.call_logs SET provider_call_id = left(p_provider_id, 256),
        transcript_ciphertext = p_ciphertext, recording_content = p_recording, outcome = p_outcome,
        started_at = COALESCE(started_at, now()),
        ended_at = CASE WHEN p_outcome = 'initiated' THEN NULL ELSE now() END,
        status = CASE WHEN p_outcome = 'initiated' THEN 'ringing'
            WHEN p_outcome LIKE 'human_%' OR p_outcome LIKE '%fallback%' THEN 'human_fallback'
            WHEN p_outcome = 'failed' THEN 'failed' ELSE 'completed' END
      WHERE id = j.call_log_id;
    INSERT INTO public.complaint_events(complaint_id, complaint_created_at, event_type,
        actor_staff_id, metadata)
      VALUES(j.complaint_id, j.complaint_created_at,
        CASE WHEN p_outcome = 'initiated' THEN 'voice_call_started' ELSE 'voice_call_completed' END,
        j.sender_staff_id, jsonb_build_object('call_log_id', j.call_log_id, 'outcome', p_outcome));
    UPDATE public.integration_jobs SET state = 'sent', provider_message_id = p_provider_id,
        finished_at = now(), locked_until = NULL, last_error_code = NULL WHERE id = p_id;
    RETURN true;
END;
$function$;

-- 7. Export artifacts are written to and read from the job row ---------------------------
DROP FUNCTION app_security.finish_export(uuid, integer, boolean, text, jsonb);
CREATE FUNCTION app_security.finish_export(
    p_job uuid, p_attempt integer, p_success boolean, p_content bytea DEFAULT NULL,
    p_manifest jsonb DEFAULT '[]'::jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_owner uuid; v_kind text;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_attempt NOT BETWEEN 1 AND 3
       OR (p_success AND (p_content IS NULL
           OR octet_length(p_content) NOT BETWEEN 31 AND 2000031
           OR jsonb_typeof(p_manifest) <> 'array'
           OR jsonb_array_length(p_manifest) > 1000)) THEN RETURN false; END IF;
    UPDATE public.export_jobs j SET status = CASE WHEN p_success THEN 'completed' ELSE 'failed' END,
        content = CASE WHEN p_success THEN p_content ELSE NULL END,
        completed_at = now(), expires_at = CASE WHEN p_success
            THEN now() + interval '15 minutes' ELSE NULL END
        WHERE j.id = p_job AND j.status = 'running' AND j.attempts = p_attempt
        RETURNING j.owner_staff_id, j.kind INTO v_owner, v_kind;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_success THEN
        INSERT INTO public.export_job_rows(job_id, complaint_id, complaint_created_at)
            SELECT p_job, (item->>'id')::uuid, (item->>'created_at')::timestamptz
            FROM jsonb_array_elements(p_manifest) item;
        INSERT INTO public.notifications(recipient_staff_id, kind, payload)
            VALUES (v_owner, 'export_ready', jsonb_build_object('job_id', p_job));
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id, after_state)
        VALUES (NULL, CASE WHEN p_success THEN 'export_completed' ELSE 'export_failed' END,
            'export_job', p_job, jsonb_build_object('kind', v_kind,
                'count', CASE WHEN p_success THEN jsonb_array_length(p_manifest) ELSE 0 END));
    RETURN true;
END;
$function$;

DROP FUNCTION app_security.authorize_export_download(uuid);
CREATE FUNCTION app_security.authorize_export_download(p_job uuid)
RETURNS TABLE (content bytea, format text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_job export_jobs%ROWTYPE;
BEGIN
    IF NOT app_security.is_active_staff() THEN RETURN; END IF;
    SELECT * INTO v_job FROM public.export_jobs j
        WHERE j.id = p_job AND j.owner_staff_id = app_security.actor_id()
            AND j.status = 'completed' AND j.expires_at > now() FOR SHARE;
    IF NOT FOUND OR v_job.content IS NULL OR NOT app_security.has_permission('complaints.view')
       OR (v_job.kind = 'complaints' AND NOT app_security.has_permission('complaints.export'))
       OR (v_job.kind = 'analytics' AND NOT
           (app_security.has_permission('analytics.view')
            AND app_security.has_permission('analytics.export')))
       OR EXISTS (SELECT 1 FROM public.export_job_rows r WHERE r.job_id = p_job
            AND NOT app_security.can_read_complaint_id(r.complaint_id,
                r.complaint_created_at)) THEN RETURN;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id, after_state)
        VALUES (app_security.actor_id(), 'export_downloaded', 'export_job', p_job,
            jsonb_build_object('kind', v_job.kind, 'format', v_job.format));
    RETURN QUERY SELECT v_job.content, v_job.format;
END;
$function$;

DROP FUNCTION app_security.claim_export_cleanup(integer);
CREATE FUNCTION app_security.claim_export_cleanup(p_limit integer DEFAULT 20)
RETURNS TABLE (job_id uuid, has_content boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 50 THEN RETURN; END IF;
    RETURN QUERY WITH expired AS (
        SELECT j.id FROM public.export_jobs j WHERE j.content IS NOT NULL
          AND (j.status = 'expired' OR
               (j.status = 'completed' AND j.expires_at <= now()))
        ORDER BY j.created_at LIMIT p_limit FOR UPDATE SKIP LOCKED
    ), marked AS (
        UPDATE public.export_jobs j SET status = 'expired'
        FROM expired WHERE expired.id = j.id
        RETURNING j.id, j.content IS NOT NULL AS has_content
    ) SELECT m.id, m.has_content FROM marked m;
END;
$function$;

DROP FUNCTION app_security.finish_export_cleanup(uuid, text);
CREATE FUNCTION app_security.finish_export_cleanup(p_job uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker' THEN
        RETURN false;
    END IF;
    UPDATE public.export_jobs SET content = NULL, private_storage_key = NULL
        WHERE id = p_job AND status = 'expired';
    RETURN FOUND;
END;
$function$;

-- 8. Staff storage inventory reads the tables, never a bucket ----------------------------
-- Bounded, metadata-only listing for the first-party console. Content bytes are
-- deliberately excluded: files keep flowing through their scoped, audited routes.
CREATE FUNCTION app_security.storage_inventory(
    p_kind text, p_after uuid, p_limit integer)
RETURNS TABLE (kind text, object_id uuid, object_key text, size_bytes bigint,
               last_modified timestamptz, checksum_sha256 text, details jsonb)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'staff' OR NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('storage.manage')
       OR p_kind NOT IN ('attachment', 'export', 'recording')
       OR p_limit NOT BETWEEN 1 AND 200 THEN RETURN; END IF;
    IF p_kind = 'attachment' THEN
        RETURN QUERY
        SELECT 'attachment'::text, a.id, 'attachments/' || a.id::text,
            COALESCE(octet_length(a.content), a.size_bytes), a.created_at, a.checksum_sha256,
            jsonb_build_object('filename', a.original_filename, 'mime_type', a.mime_type,
                'declared_size_bytes', a.size_bytes, 'scan_status', a.scan_status,
                'visibility', a.visibility, 'encrypted', a.content IS NOT NULL,
                'complaint_id', a.complaint_id, 'ticket_no', k.ticket_no)
        FROM public.attachments a
        JOIN public.complaint_keys k ON k.id = a.complaint_id
        WHERE (p_after IS NULL OR a.id > p_after)
        ORDER BY a.id LIMIT p_limit;
        RETURN;
    END IF;
    IF p_kind = 'export' THEN
        RETURN QUERY
        SELECT 'export'::text, j.id, 'exports/' || j.id::text,
            COALESCE(octet_length(j.content), 0), j.completed_at, NULL::text,
            jsonb_build_object('export_kind', j.kind, 'format', j.format, 'status', j.status,
                'owner_staff_id', j.owner_staff_id, 'expires_at', j.expires_at,
                'encrypted', j.content IS NOT NULL)
        FROM public.export_jobs j
        WHERE j.content IS NOT NULL AND (p_after IS NULL OR j.id > p_after)
        ORDER BY j.id LIMIT p_limit;
        RETURN;
    END IF;
    RETURN QUERY
    SELECT 'recording'::text, l.id, 'recordings/' || l.id::text,
        COALESCE(octet_length(l.recording_content), 0),
        COALESCE(l.ended_at, l.started_at), NULL::text,
        jsonb_build_object('complaint_id', l.complaint_id, 'direction', l.direction,
            'status', l.status, 'provider', l.provider,
            'transcript_available', l.transcript_ciphertext IS NOT NULL,
            'encrypted', l.recording_content IS NOT NULL)
    FROM public.call_logs l
    WHERE l.recording_content IS NOT NULL AND (p_after IS NULL OR l.id > p_after)
    ORDER BY l.id LIMIT p_limit;
END;
$function$;

CREATE FUNCTION app_security.storage_totals()
RETURNS TABLE (kind text, objects bigint, bytes bigint, attention bigint, missing_content bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'staff' OR NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('storage.manage') THEN RETURN; END IF;
    RETURN QUERY
    SELECT 'attachment'::text, count(*)::bigint,
        COALESCE(sum(COALESCE(octet_length(a.content), a.size_bytes)), 0)::bigint,
        count(*) FILTER (WHERE a.scan_status = 'pending')::bigint,
        count(*) FILTER (WHERE a.content IS NULL)::bigint
      FROM public.attachments a
    UNION ALL
    SELECT 'export'::text, count(*)::bigint,
        COALESCE(sum(octet_length(j.content)), 0)::bigint,
        count(*) FILTER (WHERE j.status IN ('queued', 'running'))::bigint, 0::bigint
      FROM public.export_jobs j WHERE j.content IS NOT NULL
    UNION ALL
    SELECT 'recording'::text, count(*)::bigint,
        COALESCE(sum(octet_length(l.recording_content)), 0)::bigint, 0::bigint, 0::bigint
      FROM public.call_logs l WHERE l.recording_content IS NOT NULL;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.storage_inventory(text, uuid, integer),
    app_security.storage_totals(),
    app_security.authorize_export_download(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.pending_scans(integer),
    app_security.attachment_scan_content(uuid, uuid),
    app_security.record_scan_verdict(uuid, text),
    app_security.clean_job_attachments(uuid, integer, uuid[]),
    app_security.create_call_job(uuid, integer, text, text, bytea, bytea),
    app_security.finish_export(uuid, integer, boolean, bytea, jsonb),
    app_security.claim_export_cleanup(integer),
    app_security.finish_export_cleanup(uuid) TO app_worker;

-- The migration role owns the definer functions; keep the application roles unable to
-- swap them. app_runtime/app_worker never receive table privileges on these columns:
-- every byte crosses a purpose-built, audited function boundary.
REVOKE ALL ON SCHEMA app_security FROM PUBLIC;

-- 9. Inbound call recordings land on the call log row as encrypted bytes; the
--    payload carries the ciphertext instead of an object-store key.
CREATE OR REPLACE FUNCTION app_security.complete_inbound_event(p_id uuid,p_attempt integer,p_data jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE; v_complaint public.complaints%ROWTYPE;
    v_message uuid; v_channel public.communication_channel; v_direction public.message_direction;
    v_transcript bytea; v_recording bytea;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_data IS NULL OR jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id=p_id AND q.state='processing'
       AND q.attempts=p_attempt AND q.job_kind IN
         ('inbound_email','inbound_whatsapp','whatsapp_status','email_status',
          'inbound_voice','voice_status') FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF j.job_kind='voice_status' THEN
        UPDATE public.call_logs l SET
            status=CASE lower(p_data->>'status')
                WHEN 'ringing' THEN 'ringing'
                WHEN 'answered' THEN 'in_progress'
                WHEN 'in-progress' THEN 'in_progress'
                WHEN 'in_progress' THEN 'in_progress'
                WHEN 'completed' THEN 'completed'
                WHEN 'failed' THEN 'failed'
                WHEN 'busy' THEN 'human_fallback'
                WHEN 'no-answer' THEN 'human_fallback'
                WHEN 'canceled' THEN 'human_fallback'
                ELSE l.status END,
            outcome=left(COALESCE(p_data->>'outcome',p_data->>'status','unknown'),80),
            started_at=CASE WHEN lower(p_data->>'status') IN ('answered','in-progress','in_progress')
                THEN COALESCE(l.started_at,now()) ELSE l.started_at END,
            ended_at=CASE WHEN lower(p_data->>'status') IN
                ('completed','failed','busy','no-answer','canceled') THEN COALESCE(l.ended_at,now())
                ELSE l.ended_at END
          FROM public.complaints c
          WHERE l.provider_call_id=p_data->>'provider_call_id' AND l.provider=j.provider
            AND c.id=l.complaint_id AND c.created_at=l.complaint_created_at
            AND c.company_id=j.company_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'unknown provider call' USING ERRCODE='P0002'; END IF;
    ELSIF j.job_kind='whatsapp_status' THEN
        UPDATE public.complaint_messages m SET
            delivery_status=CASE WHEN p_data->>'status'='read' THEN 'read'
                WHEN m.delivery_status='read' THEN 'read' ELSE 'delivered' END,
            delivered_at=COALESCE(m.delivered_at,now()),
            read_at=CASE WHEN p_data->>'status'='read' THEN COALESCE(m.read_at,now()) ELSE m.read_at END
          FROM public.complaints c
          WHERE m.channel='whatsapp' AND m.external_message_id=p_data->>'provider_message_id'
            AND c.id=m.complaint_id AND c.created_at=m.complaint_created_at
            AND c.company_id=j.company_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'unknown provider message' USING ERRCODE='P0002'; END IF;
    ELSIF j.job_kind='email_status' THEN
        IF COALESCE(p_data->>'status','') NOT IN ('delivered','failed')
           OR length(COALESCE(p_data->>'provider_message_id','')) NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION 'email delivery event invalid' USING ERRCODE='22023';
        END IF;
        UPDATE public.complaint_messages m SET
            delivery_status=p_data->>'status',
            delivered_at=CASE WHEN p_data->>'status'='delivered'
                THEN COALESCE(m.delivered_at,now()) ELSE m.delivered_at END
          FROM public.complaints c
          WHERE m.channel='email' AND m.external_message_id=p_data->>'provider_message_id'
            AND c.id=m.complaint_id AND c.created_at=m.complaint_created_at
            AND c.company_id=j.company_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'unknown provider message' USING ERRCODE='P0002'; END IF;
    ELSIF j.job_kind IN ('inbound_email','inbound_whatsapp') THEN
        SELECT * INTO v_complaint FROM public.complaints c
          WHERE c.ticket_no=p_data->>'ticket_no' AND c.company_id=j.company_id LIMIT 1;
        IF NOT FOUND THEN RAISE EXCEPTION 'ticket not found' USING ERRCODE='P0002'; END IF;
        IF length(btrim(COALESCE(p_data->>'body',''))) NOT BETWEEN 1 AND 5000
           OR length(COALESCE(p_data->>'external_message_id','')) NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION 'inbound message invalid' USING ERRCODE='22023';
        END IF;
        v_channel:=CASE WHEN j.job_kind='inbound_email' THEN 'email'::public.communication_channel
            ELSE 'whatsapp'::public.communication_channel END;
        v_direction:='inbound';
        IF v_channel='whatsapp' AND NOT EXISTS (SELECT 1 FROM public.customers c
             WHERE c.id=v_complaint.customer_id AND c.whatsapp_available
               AND c.mobile=p_data->>'sender') THEN
            RAISE EXCEPTION 'whatsapp customer mismatch or no opt-in' USING ERRCODE='42501';
        END IF;
        INSERT INTO public.complaint_messages(complaint_id,complaint_created_at,body,visibility,
            channel,direction,sender_customer_id,sender_external,external_message_id,delivery_status)
          VALUES(v_complaint.id,v_complaint.created_at,left(p_data->>'body',5000),'public',
            v_channel,v_direction,
            CASE WHEN v_channel='whatsapp' THEN v_complaint.customer_id ELSE NULL END,
            left(COALESCE(p_data->>'sender',''),254),p_data->>'external_message_id','received')
          ON CONFLICT (channel,external_message_id) WHERE external_message_id IS NOT NULL DO NOTHING
          RETURNING id INTO v_message;
        IF v_message IS NOT NULL THEN
            INSERT INTO public.complaint_events(complaint_id,complaint_created_at,event_type,
                metadata)
              VALUES(v_complaint.id,v_complaint.created_at,
                CASE WHEN v_channel='email' THEN 'inbound_email' ELSE 'inbound_whatsapp' END,
                jsonb_build_object('message_id',v_message,'channel',v_channel));
        END IF;
    ELSE
        SELECT * INTO v_complaint FROM public.complaints c
          WHERE c.ticket_no=p_data->>'ticket_no' AND c.company_id=j.company_id LIMIT 1;
        IF NOT FOUND THEN RAISE EXCEPTION 'ticket not found' USING ERRCODE='P0002'; END IF;
        IF p_data->>'transcript_ciphertext' IS NOT NULL THEN
            v_transcript:=decode(p_data->>'transcript_ciphertext','base64');
        END IF;
        IF p_data->>'recording_ciphertext' IS NOT NULL THEN
            v_recording:=decode(p_data->>'recording_ciphertext','base64');
            IF octet_length(v_recording) NOT BETWEEN 31 AND 26214431 THEN
                RAISE EXCEPTION 'inbound recording invalid' USING ERRCODE='22023';
            END IF;
        END IF;
        INSERT INTO public.call_logs(id,complaint_id,complaint_created_at,direction,provider_call_id,
            transcript_ciphertext,recording_content,outcome,consent_at,started_at,ended_at,status,provider)
          VALUES((p_data->>'call_log_id')::uuid,v_complaint.id,v_complaint.created_at,'inbound',
            left(p_data->>'provider_call_id',256),
            v_transcript,v_recording,
            left(COALESCE(p_data->>'outcome','human_fallback'),80),
            CASE WHEN COALESCE((p_data->>'consent_confirmed')::boolean,false) THEN now() ELSE NULL END,
            now(),now(),CASE WHEN NOT COALESCE((p_data->>'consent_confirmed')::boolean,false)
                OR COALESCE(p_data->>'outcome','') LIKE 'human_%'
                OR COALESCE(p_data->>'outcome','') LIKE '%fallback%'
                THEN 'human_fallback' ELSE 'completed' END,j.provider);
        INSERT INTO public.complaint_events(complaint_id,complaint_created_at,event_type,
            metadata)
          VALUES(v_complaint.id,v_complaint.created_at,'inbound_voice_call',
            jsonb_build_object('outcome',COALESCE(p_data->>'outcome','human_fallback'),
                               'consent_confirmed',COALESCE((p_data->>'consent_confirmed')::boolean,false)));
    END IF;
    UPDATE public.integration_jobs SET state='sent',finished_at=now(),locked_until=NULL,
       last_error_code=NULL WHERE id=p_id;
    RETURN true;
END;
$function$;
