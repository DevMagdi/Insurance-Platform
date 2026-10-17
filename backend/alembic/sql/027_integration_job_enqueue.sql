-- Queueing an outbound integration job could never succeed: the Phase 4
-- ``create_integration_job`` body used an unqualified ``RETURNING id, state``
-- while ``state`` is also the function's OUT column, so PostgreSQL refused the
-- statement at run time (42702 "column reference \"state\" is ambiguous"). Every
-- send_email / send_whatsapp / ai_analyze / voice_followup enqueue therefore
-- failed with 503 "Integration queue is unavailable". Qualify the RETURNING
-- columns through a table alias so the column is never confused with the PL/pgSQL
-- variable; the function signature, grants and caller contract are unchanged.
CREATE OR REPLACE FUNCTION app_security.create_integration_job(
    p_job_kind text, p_company uuid, p_complaint uuid, p_created timestamptz,
    p_sender uuid, p_message uuid, p_call uuid, p_ciphertext bytea,
    p_idempotency bytea, p_payload_digest bytea, p_scheduled_at timestamptz DEFAULT NULL)
RETURNS TABLE (job_id uuid, created boolean, state text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_permission text; v_providers text[]; v_config public.integrations_config%ROWTYPE;
    v_id uuid; v_created boolean := false; v_state text; v_existing_digest bytea;
BEGIN
    v_permission := CASE p_job_kind
        WHEN 'send_email' THEN 'integrations.send_email'
        WHEN 'send_whatsapp' THEN 'integrations.send_whatsapp'
        WHEN 'ai_analyze' THEN 'integrations.ai_call'
        WHEN 'voice_followup' THEN 'integrations.ai_call'
        ELSE NULL END;
    v_providers := CASE p_job_kind
        WHEN 'send_email' THEN ARRAY['smtp','sendgrid','ses']
        WHEN 'send_whatsapp' THEN ARRAY['whatsapp_cloud']
        WHEN 'ai_analyze' THEN ARRAY['mock_llm','openai']
        WHEN 'voice_followup' THEN ARRAY['mock_voice','twilio','vonage']
        ELSE ARRAY[]::text[] END;
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'staff'
       OR v_permission IS NULL OR p_sender <> app_security.actor_id()
       OR NOT app_security.has_permission(v_permission)
       OR NOT app_security.has_permission('complaints.view')
       OR NOT app_security.can_read_complaint_id(p_complaint, p_created)
       OR octet_length(p_ciphertext) < 28 OR octet_length(p_idempotency) <> 32
       OR octet_length(p_payload_digest) <> 32
       OR NOT EXISTS (SELECT 1 FROM public.complaints c
          WHERE c.id = p_complaint AND c.created_at = p_created AND c.company_id = p_company)
       OR (p_job_kind IN ('send_email','send_whatsapp') AND p_message IS NULL)
       OR (p_job_kind = 'voice_followup' AND p_call IS NULL)
       OR (p_job_kind IN ('send_email','send_whatsapp') AND NOT EXISTS (
          SELECT 1 FROM public.complaint_messages m WHERE m.id = p_message
            AND m.complaint_id = p_complaint AND m.complaint_created_at = p_created
            AND m.sender_staff_id = p_sender))
       OR (p_job_kind = 'voice_followup' AND NOT EXISTS (
          SELECT 1 FROM public.call_logs c WHERE c.id = p_call
            AND c.complaint_id = p_complaint AND c.complaint_created_at = p_created
            AND c.sender_staff_id = p_sender AND c.consent_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'integration action forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT j.id,j.state,j.payload_digest INTO v_id,v_state,v_existing_digest
      FROM public.integration_jobs j WHERE j.idempotency_digest = p_idempotency;
    IF FOUND THEN
        IF v_existing_digest IS DISTINCT FROM p_payload_digest THEN
            RAISE EXCEPTION 'idempotency key reused with different payload' USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT v_id,false,v_state;
        RETURN;
    END IF;
    SELECT i.* INTO v_config FROM public.integrations_config i
      WHERE i.provider = ANY(v_providers) AND (i.company_id = p_company OR i.company_id IS NULL)
      ORDER BY (i.company_id IS NOT NULL) DESC, i.updated_at DESC LIMIT 1;
    IF NOT FOUND OR NOT v_config.is_enabled OR v_config.config_ciphertext IS NULL THEN
        RAISE EXCEPTION 'integration provider is not connected' USING ERRCODE = '55000';
    END IF;
    INSERT INTO public.integration_jobs AS queued_job(job_kind,provider,company_id,complaint_id,
        complaint_created_at,sender_staff_id,integration_config_id,message_id,call_log_id,
        payload_ciphertext,idempotency_digest,payload_digest,available_at)
      VALUES(p_job_kind,v_config.provider,p_company,p_complaint,p_created,p_sender,
        v_config.id,p_message,p_call,p_ciphertext,p_idempotency,p_payload_digest,
        COALESCE(p_scheduled_at,now()))
      ON CONFLICT (idempotency_digest) DO NOTHING
      RETURNING queued_job.id,queued_job.state INTO v_id,v_state;
    v_created:=FOUND;
    IF NOT v_created THEN
        SELECT j.id,j.state,j.payload_digest INTO v_id,v_state,v_existing_digest
          FROM public.integration_jobs j WHERE j.idempotency_digest=p_idempotency;
        IF v_existing_digest IS DISTINCT FROM p_payload_digest THEN
            RAISE EXCEPTION 'idempotency key reused with different payload' USING ERRCODE = '23505';
        END IF;
        RETURN QUERY SELECT v_id,false,v_state;
        RETURN;
    END IF;
    IF p_call IS NOT NULL THEN
        UPDATE public.call_logs SET provider=v_config.provider,status='queued'
          WHERE id=p_call AND sender_staff_id=p_sender;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,
        complaint_id,complaint_created_at,after_state)
      VALUES(p_sender,p_job_kind,'integration_job',v_id,p_complaint,p_created,
        jsonb_build_object('channel',p_job_kind,'sender_staff_id',p_sender));
    RETURN QUERY SELECT v_id,true,v_state;
END;
$function$;
