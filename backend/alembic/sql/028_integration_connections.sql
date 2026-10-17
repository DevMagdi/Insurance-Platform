-- Real, testable provider connections: connection-test status, Gmail/Outlook
-- OAuth providers, Twilio SMS with delivery callbacks, OpenAI speech, ClamAV
-- scanning and an SMS message channel. Secrets remain AES-GCM encrypted by the
-- API/worker with APP_ENCRYPTION_KEY; this revision never stores or returns
-- plaintext credentials, and it retires the ``secret_ref`` vault-name path that
-- could never prove a working connection.

-- 1. Connection-test lifecycle on integrations_config -----------------------------
-- unconfigured: no credentials; configured: credentials saved, not yet tested;
-- connected: the last connection test succeeded; failed: it failed.
ALTER TABLE public.integrations_config
    ADD COLUMN connection_status text NOT NULL DEFAULT 'unconfigured',
    ADD COLUMN last_test_at timestamptz,
    ADD COLUMN last_error_code text;
ALTER TABLE public.integrations_config
    ADD CONSTRAINT ck_integrations_connection_status
    CHECK (connection_status IN ('unconfigured', 'configured', 'connected', 'failed'));
ALTER TABLE public.integrations_config
    ADD CONSTRAINT ck_integrations_last_error
    CHECK (last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 80);

-- 2. SMS is a first-class customer channel; fresh installs seed it from 001. ------
ALTER TYPE public.communication_channel ADD VALUE IF NOT EXISTS 'sms';

-- 3. save_integration_config: accept the real providers and track test state. ----
-- The signature is unchanged so in-flight callers keep working during rollout.
CREATE OR REPLACE FUNCTION app_security.save_integration_config(
    p_company uuid, p_provider text, p_ciphertext bytea, p_enabled boolean,
    p_clear_credentials boolean DEFAULT false)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_old_enabled boolean;
BEGIN
    IF NOT app_security.integration_company_allowed(p_company)
       OR p_provider NOT IN ('smtp','sendgrid','ses','gmail_oauth','outlook_oauth',
                             'whatsapp_cloud','email_webhook','imap','twilio_sms',
                             'mock_llm','openai','mock_voice','twilio','vonage',
                             'openai_speech','clamav')
       OR (p_provider IN ('email_webhook','imap') AND p_company IS NULL)
       OR (p_provider = 'clamav' AND p_company IS NOT NULL)
       OR (p_ciphertext IS NOT NULL AND octet_length(p_ciphertext) < 28)
       OR (p_clear_credentials AND (p_ciphertext IS NOT NULL OR p_enabled))
       OR (p_enabled AND p_ciphertext IS NULL AND NOT EXISTS (
             SELECT 1 FROM public.integrations_config i
             WHERE i.company_id IS NOT DISTINCT FROM p_company AND i.provider = p_provider
               AND i.config_ciphertext IS NOT NULL)) THEN
        RAISE EXCEPTION 'integration configuration forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    SELECT i.is_enabled INTO v_old_enabled FROM public.integrations_config i
        WHERE i.company_id IS NOT DISTINCT FROM p_company AND i.provider = p_provider;
    INSERT INTO public.integrations_config(company_id, provider, config_ciphertext,
        secret_ref, is_enabled, connection_status, last_test_at, last_error_code, updated_at)
        VALUES (p_company,p_provider,CASE WHEN p_clear_credentials THEN NULL ELSE p_ciphertext END,
            CASE WHEN p_clear_credentials THEN NULL ELSE 'encrypted-at-rest' END,p_enabled,
            CASE WHEN p_clear_credentials THEN 'unconfigured' ELSE 'configured' END,
            NULL, NULL, now())
        ON CONFLICT (company_id, provider) DO UPDATE SET
            config_ciphertext=CASE WHEN p_clear_credentials THEN NULL ELSE COALESCE(
                EXCLUDED.config_ciphertext,public.integrations_config.config_ciphertext) END,
            secret_ref=CASE WHEN p_clear_credentials THEN NULL ELSE 'encrypted-at-rest' END,
            is_enabled=EXCLUDED.is_enabled,
            connection_status=CASE WHEN p_clear_credentials THEN 'unconfigured'
                WHEN EXCLUDED.config_ciphertext IS NOT NULL THEN 'configured'
                ELSE public.integrations_config.connection_status END,
            last_test_at=CASE WHEN p_clear_credentials OR EXCLUDED.config_ciphertext IS NOT NULL
                THEN NULL ELSE public.integrations_config.last_test_at END,
            last_error_code=CASE WHEN p_clear_credentials OR EXCLUDED.config_ciphertext IS NOT NULL
                THEN NULL ELSE public.integrations_config.last_error_code END,
            updated_at = now()
        RETURNING id INTO v_id;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        before_state, after_state)
        VALUES (app_security.actor_id(), 'integration_config_updated', 'integration', v_id,
            jsonb_build_object('is_enabled', COALESCE(v_old_enabled, false)),
            jsonb_build_object('provider', p_provider, 'company_id', p_company,
                               'is_enabled', p_enabled));
    RETURN v_id;
END;
$function$;

-- 4. list_integration_configs exposes status fields and never credentials. -------
DROP FUNCTION app_security.list_integration_configs();
CREATE FUNCTION app_security.list_integration_configs()
RETURNS TABLE (id uuid, company_id uuid, provider text, is_enabled boolean,
               has_credentials boolean, updated_at timestamptz,
               connection_status text, last_test_at timestamptz, last_error_code text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'staff'
       OR NOT app_security.has_permission('integrations.manage') THEN RETURN; END IF;
    RETURN QUERY SELECT i.id, i.company_id, i.provider, i.is_enabled,
        i.config_ciphertext IS NOT NULL, i.updated_at,
        CASE WHEN i.config_ciphertext IS NULL THEN 'unconfigured' ELSE i.connection_status END,
        i.last_test_at, i.last_error_code
      FROM public.integrations_config i
      WHERE (i.company_id IS NULL AND app_security.has_global_scope())
         OR (i.company_id IS NOT NULL AND app_security.integration_company_allowed(i.company_id))
      ORDER BY i.company_id NULLS FIRST, i.provider;
END;
$function$;

-- 5. Persist connection-test outcomes (admin-triggered, audited). ----------------
CREATE FUNCTION app_security.record_integration_connection_test(
    p_id uuid, p_status text, p_error_code text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_row public.integrations_config%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'staff'
       OR NOT app_security.has_permission('integrations.manage')
       OR p_status NOT IN ('unconfigured','configured','connected','failed')
       OR (p_status = 'failed' AND COALESCE(p_error_code,'') NOT SIMILAR TO '[a-z0-9_:-]{1,80}')
       OR (p_status <> 'failed' AND p_error_code IS NOT NULL) THEN
        RETURN false;
    END IF;
    SELECT * INTO v_row FROM public.integrations_config i WHERE i.id = p_id;
    IF NOT FOUND OR v_row.config_ciphertext IS NULL THEN RETURN false; END IF;
    IF NOT ((v_row.company_id IS NULL AND app_security.has_global_scope())
        OR (v_row.company_id IS NOT NULL
            AND app_security.integration_company_allowed(v_row.company_id))) THEN
        RETURN false;
    END IF;
    UPDATE public.integrations_config i SET connection_status = p_status,
        last_test_at = now(),
        last_error_code = CASE WHEN p_status = 'failed' THEN p_error_code ELSE NULL END,
        updated_at = CASE WHEN p_status = 'unconfigured' THEN i.updated_at ELSE now() END
      WHERE i.id = p_id;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, resource_id,
        before_state, after_state)
      VALUES (app_security.actor_id(), 'integration_connection_tested', 'integration', p_id,
        jsonb_build_object('provider', v_row.provider,
                           'connection_status', v_row.connection_status),
        jsonb_build_object('provider', v_row.provider, 'company_id', v_row.company_id,
                           'connection_status', p_status,
                           'error_code', COALESCE(p_error_code, '')));
    RETURN true;
END;
$function$;

-- 6. Purpose-scoped credential access for admin connection tests and the worker.
-- ``p_allow_global_fallback`` selects the effective (company-or-global) config the
-- same way job dispatch does; admin tests pass false to probe one exact row.
CREATE FUNCTION app_security.integration_credentials(
    p_company uuid, p_provider text, p_allow_global_fallback boolean DEFAULT true)
RETURNS TABLE (config_id uuid, config_company_id uuid, config_ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT ((session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR (session_user = 'app_runtime' AND app_security.actor_scope() = 'staff'
            AND app_security.has_permission('integrations.manage')
            AND ((p_company IS NULL AND app_security.has_global_scope())
                 OR app_security.integration_company_allowed(p_company)))) THEN
        RETURN;
    END IF;
    IF p_provider NOT IN ('smtp','sendgrid','ses','gmail_oauth','outlook_oauth',
                          'whatsapp_cloud','email_webhook','imap','twilio_sms',
                          'mock_llm','openai','mock_voice','twilio','vonage',
                          'openai_speech','clamav') THEN
        RETURN;
    END IF;
    RETURN QUERY SELECT candidate.id, candidate.company_id, candidate.config_ciphertext
      FROM (SELECT i.id, i.company_id, i.config_ciphertext FROM public.integrations_config i
        WHERE i.provider = p_provider AND i.is_enabled AND i.config_ciphertext IS NOT NULL
          AND (i.company_id IS NOT DISTINCT FROM p_company
               OR (p_allow_global_fallback AND p_company IS NOT NULL AND i.company_id IS NULL))
        ORDER BY (i.company_id IS NOT NULL) DESC, i.updated_at DESC LIMIT 1) candidate;
END;
$function$;

-- 7. Public webhooks: Twilio SMS callbacks use the same signed form scheme. ------
CREATE OR REPLACE FUNCTION app_security.webhook_integration_config(p_company uuid, p_provider text)
RETURNS TABLE (config_company_id uuid, config_ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_cipher bytea; v_enabled boolean; v_scope uuid;
BEGIN
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'webhook'
       OR p_provider NOT IN ('email_webhook','whatsapp_cloud','mock_voice','twilio','twilio_sms',
                             'vonage')
       OR NOT EXISTS (SELECT 1 FROM public.insurance_companies c
                      WHERE c.id = p_company AND c.is_active) THEN RETURN; END IF;
    SELECT candidate.company_id,candidate.config_ciphertext,candidate.is_enabled
      INTO v_scope,v_cipher,v_enabled
      FROM (SELECT i.company_id,i.config_ciphertext,i.is_enabled FROM public.integrations_config i
        WHERE i.provider = p_provider AND (i.company_id = p_company OR i.company_id IS NULL)
        ORDER BY (i.company_id IS NOT NULL) DESC, i.updated_at DESC LIMIT 1) candidate;
    IF NOT FOUND OR NOT v_enabled THEN RETURN; END IF;
    RETURN QUERY SELECT v_scope,v_cipher;
END;
$function$;

-- 8. Inbound queue: SMS deliveries and inbound SMS events. -----------------------
CREATE OR REPLACE FUNCTION app_security.enqueue_inbound_job(
    p_company uuid, p_provider text, p_job_kind text, p_ciphertext bytea,
    p_idempotency bytea, p_payload_digest bytea)
RETURNS TABLE (job_id uuid, created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_created boolean := false;
BEGIN
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'webhook'
       OR p_job_kind NOT IN ('inbound_email','inbound_whatsapp','whatsapp_status',
                             'email_status','inbound_voice','voice_status',
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
        ON CONFLICT (idempotency_digest) DO NOTHING RETURNING id INTO v_id;
    v_created := FOUND;
    IF NOT v_created THEN
        SELECT j.id INTO v_id FROM public.integration_jobs j
            WHERE j.idempotency_digest = p_idempotency AND j.payload_digest = p_payload_digest;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'idempotency key reused with different payload' USING ERRCODE = '23505';
        END IF;
    END IF;
    RETURN QUERY SELECT v_id, v_created;
END;
$function$;

-- 9. Outbound queue: send_sms plus the Gmail/Outlook OAuth transports. -----------
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
        WHEN 'send_sms' THEN 'integrations.send_sms'
        WHEN 'ai_analyze' THEN 'integrations.ai_call'
        WHEN 'voice_followup' THEN 'integrations.ai_call'
        ELSE NULL END;
    v_providers := CASE p_job_kind
        WHEN 'send_email' THEN ARRAY['smtp','sendgrid','ses','gmail_oauth','outlook_oauth']
        WHEN 'send_whatsapp' THEN ARRAY['whatsapp_cloud']
        WHEN 'send_sms' THEN ARRAY['twilio_sms']
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
       OR (p_job_kind IN ('send_email','send_whatsapp','send_sms') AND p_message IS NULL)
       OR (p_job_kind = 'voice_followup' AND p_call IS NULL)
       OR (p_job_kind IN ('send_email','send_whatsapp','send_sms') AND NOT EXISTS (
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

-- 10. Inbound completion: SMS delivery callbacks and inbound customer SMS. ------
CREATE OR REPLACE FUNCTION app_security.complete_inbound_event(p_id uuid,p_attempt integer,p_data jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE; v_complaint public.complaints%ROWTYPE;
    v_message uuid; v_channel public.communication_channel; v_direction public.message_direction;
    v_transcript bytea;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_data IS NULL OR jsonb_typeof(p_data) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id=p_id AND q.state='processing'
       AND q.attempts=p_attempt AND q.job_kind IN
         ('inbound_email','inbound_whatsapp','whatsapp_status','email_status',
          'inbound_voice','voice_status','inbound_sms','sms_status') FOR UPDATE;
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
    ELSIF j.job_kind='sms_status' THEN
        -- Twilio MessageStatus callbacks: queued/sending/sent/delivered/undelivered/failed/read.
        IF COALESCE(p_data->>'status','') NOT IN
             ('queued','sending','sent','delivered','undelivered','failed','read')
           OR length(COALESCE(p_data->>'provider_message_id','')) NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION 'sms delivery event invalid' USING ERRCODE='22023';
        END IF;
        UPDATE public.complaint_messages m SET
            delivery_status=CASE
                WHEN p_data->>'status' IN ('delivered','read') THEN p_data->>'status'
                WHEN p_data->>'status' IN ('undelivered','failed') THEN 'failed'
                WHEN m.delivery_status IN ('delivered','read','failed') THEN m.delivery_status
                ELSE 'sent' END,
            delivered_at=CASE WHEN p_data->>'status' IN ('delivered','read')
                THEN COALESCE(m.delivered_at,now()) ELSE m.delivered_at END,
            read_at=CASE WHEN p_data->>'status'='read' THEN COALESCE(m.read_at,now()) ELSE m.read_at END
          FROM public.complaints c
          WHERE m.channel='sms' AND m.external_message_id=p_data->>'provider_message_id'
            AND c.id=m.complaint_id AND c.created_at=m.complaint_created_at
            AND c.company_id=j.company_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'unknown provider message' USING ERRCODE='P0002'; END IF;
    ELSIF j.job_kind IN ('inbound_email','inbound_whatsapp','inbound_sms') THEN
        SELECT * INTO v_complaint FROM public.complaints c
          WHERE c.ticket_no=p_data->>'ticket_no' AND c.company_id=j.company_id LIMIT 1;
        IF NOT FOUND THEN RAISE EXCEPTION 'ticket not found' USING ERRCODE='P0002'; END IF;
        IF length(btrim(COALESCE(p_data->>'body',''))) NOT BETWEEN 1 AND 5000
           OR length(COALESCE(p_data->>'external_message_id','')) NOT BETWEEN 1 AND 256 THEN
            RAISE EXCEPTION 'inbound message invalid' USING ERRCODE='22023';
        END IF;
        v_channel:=CASE WHEN j.job_kind='inbound_email' THEN 'email'::public.communication_channel
            WHEN j.job_kind='inbound_sms' THEN 'sms'::public.communication_channel
            ELSE 'whatsapp'::public.communication_channel END;
        v_direction:='inbound';
        IF v_channel='whatsapp' AND NOT EXISTS (SELECT 1 FROM public.customers c
             WHERE c.id=v_complaint.customer_id AND c.whatsapp_available
               AND c.mobile=p_data->>'sender') THEN
            RAISE EXCEPTION 'whatsapp customer mismatch or no opt-in' USING ERRCODE='42501';
        END IF;
        IF v_channel='sms' AND NOT EXISTS (SELECT 1 FROM public.customers c
             WHERE c.id=v_complaint.customer_id AND c.mobile=p_data->>'sender') THEN
            RAISE EXCEPTION 'sms customer mismatch' USING ERRCODE='42501';
        END IF;
        INSERT INTO public.complaint_messages(complaint_id,complaint_created_at,body,visibility,
            channel,direction,sender_customer_id,sender_external,external_message_id,delivery_status)
          VALUES(v_complaint.id,v_complaint.created_at,left(p_data->>'body',5000),'public',
            v_channel,v_direction,
            CASE WHEN v_channel IN ('whatsapp','sms') THEN v_complaint.customer_id ELSE NULL END,
            left(COALESCE(p_data->>'sender',''),254),p_data->>'external_message_id','received')
          ON CONFLICT (channel,external_message_id) WHERE external_message_id IS NOT NULL DO NOTHING
          RETURNING id INTO v_message;
        IF v_message IS NOT NULL THEN
            INSERT INTO public.complaint_events(complaint_id,complaint_created_at,event_type,
                metadata)
              VALUES(v_complaint.id,v_complaint.created_at,
                CASE WHEN v_channel='email' THEN 'inbound_email'
                     WHEN v_channel='sms' THEN 'inbound_sms'
                     ELSE 'inbound_whatsapp' END,
                jsonb_build_object('message_id',v_message,'channel',v_channel));
        END IF;
    ELSE
        SELECT * INTO v_complaint FROM public.complaints c
          WHERE c.ticket_no=p_data->>'ticket_no' AND c.company_id=j.company_id LIMIT 1;
        IF NOT FOUND THEN RAISE EXCEPTION 'ticket not found' USING ERRCODE='P0002'; END IF;
        IF p_data->>'transcript_ciphertext' IS NOT NULL THEN
            v_transcript:=decode(p_data->>'transcript_ciphertext','base64');
        END IF;
        INSERT INTO public.call_logs(id,complaint_id,complaint_created_at,direction,provider_call_id,
            transcript_ciphertext,recording_storage_key,outcome,consent_at,started_at,ended_at,status,provider)
          VALUES((p_data->>'call_log_id')::uuid,v_complaint.id,v_complaint.created_at,'inbound',
            left(p_data->>'provider_call_id',256),
            v_transcript,left(p_data->>'recording_storage_key',512),
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

-- 11. Per-company job counters for provider cost/rate limits. -------------------
CREATE FUNCTION app_security.integration_job_counts(
    p_company uuid, p_kind text, p_window_seconds integer DEFAULT 86400)
RETURNS integer LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT ((session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR (session_user = 'app_runtime' AND app_security.actor_scope() = 'staff'
            AND app_security.has_permission('integrations.manage')
            AND ((p_company IS NULL AND app_security.has_global_scope())
                 OR app_security.integration_company_allowed(p_company)))) THEN
        RETURN NULL;
    END IF;
    IF p_kind IS NULL OR p_window_seconds NOT BETWEEN 60 AND 2592000 THEN RETURN NULL; END IF;
    RETURN (SELECT count(*)::integer FROM public.integration_jobs j
        WHERE j.company_id = p_company AND j.job_kind = p_kind
          AND j.created_at >= now() - make_interval(secs => p_window_seconds));
END;
$function$;

-- 12. The send_sms permission; administrator receives it automatically. ---------
INSERT INTO permissions (code, resource, action, description)
    VALUES ('integrations.send_sms', 'integrations', 'send_sms',
            'Send customer SMS through a configured provider')
    ON CONFLICT (code) DO NOTHING;
INSERT INTO role_permissions (role_id, permission_code)
    SELECT id, 'integrations.send_sms' FROM public.roles
      WHERE name = 'administrator' AND is_system
    ON CONFLICT DO NOTHING;

-- 13. Retire the secret_ref vault-name functions: credentials live only in the
--     encrypted column that save_integration_config manages. Contract step for
--     code that no longer calls app_security.save_integration/admin_integrations/
--     delete_integration.
DROP FUNCTION IF EXISTS app_security.save_integration(uuid, jsonb);
DROP FUNCTION IF EXISTS app_security.admin_integrations();
DROP FUNCTION IF EXISTS app_security.delete_integration(uuid);

-- 14. Grants for the new and replaced functions. --------------------------------
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.save_integration_config(uuid,text,bytea,boolean,boolean),
    app_security.list_integration_configs(),
    app_security.record_integration_connection_test(uuid,text,text),
    app_security.integration_credentials(uuid,text,boolean),
    app_security.integration_job_counts(uuid,text,integer),
    app_security.webhook_integration_config(uuid,text),
    app_security.enqueue_inbound_job(uuid,text,text,bytea,bytea,bytea),
    app_security.create_integration_job(text,uuid,uuid,timestamptz,uuid,uuid,uuid,bytea,bytea,bytea,timestamptz)
    TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.complete_inbound_event(uuid,integer,jsonb),
    app_security.integration_credentials(uuid,text,boolean),
    app_security.integration_job_counts(uuid,text,integer),
    app_security.create_integration_job(text,uuid,uuid,timestamptz,uuid,uuid,uuid,bytea,bytea,bytea,timestamptz)
    TO app_worker;

-- 15. Inbound mail polling now covers Gmail/Outlook OAuth inboxes as well. ------
DROP FUNCTION app_security.claim_imap_configs(integer);
CREATE FUNCTION app_security.claim_imap_configs(p_limit integer DEFAULT 20)
RETURNS TABLE (config_id uuid, company_id uuid, provider text, config_ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    RETURN QUERY SELECT i.id,i.company_id,i.provider,i.config_ciphertext
       FROM public.integrations_config i
       WHERE i.provider IN ('imap','gmail_oauth','outlook_oauth')
         AND i.is_enabled AND i.config_ciphertext IS NOT NULL
       ORDER BY i.updated_at DESC LIMIT p_limit;
END;
$function$;

DROP FUNCTION app_security.enqueue_worker_inbound(uuid,text,bytea,bytea,bytea);
CREATE FUNCTION app_security.enqueue_worker_inbound(
    p_company uuid,p_job_kind text,p_provider text,p_ciphertext bytea,
    p_idempotency bytea,p_digest bytea)
RETURNS TABLE(job_id uuid,created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_created boolean;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_job_kind<>'inbound_email' OR p_provider NOT IN ('imap','gmail_oauth','outlook_oauth')
       OR octet_length(p_ciphertext)<28
       OR octet_length(p_idempotency)<>32 OR octet_length(p_digest)<>32 THEN
        RAISE EXCEPTION 'worker inbound queue forbidden' USING ERRCODE='42501';
    END IF;
    INSERT INTO public.integration_jobs(job_kind,provider,company_id,payload_ciphertext,
        idempotency_digest,payload_digest)
      VALUES(p_job_kind,p_provider,p_company,p_ciphertext,p_idempotency,p_digest)
      ON CONFLICT(idempotency_digest) DO NOTHING RETURNING id INTO v_id;
    v_created:=FOUND;
    IF NOT v_created THEN
        SELECT j.id INTO v_id FROM public.integration_jobs j
            WHERE j.idempotency_digest=p_idempotency AND j.payload_digest=p_digest;
        IF v_id IS NULL THEN RAISE EXCEPTION 'IMAP message idempotency conflict' USING ERRCODE='23505'; END IF;
    END IF;
    RETURN QUERY SELECT v_id,v_created;
END;
$function$;

REVOKE ALL ON FUNCTION app_security.claim_imap_configs(integer),
    app_security.enqueue_worker_inbound(uuid,text,text,bytea,bytea,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.claim_imap_configs(integer),
    app_security.enqueue_worker_inbound(uuid,text,text,bytea,bytea,bytea)
    TO app_worker;
