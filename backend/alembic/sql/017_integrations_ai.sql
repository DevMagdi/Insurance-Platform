-- Phase 4: credential lifecycle, integration outbox/DLQ, API keys and call/message state.
-- Secrets and job payloads are AES-GCM encrypted by the API/worker with the shared
-- APP_ENCRYPTION_KEY; neither runtime database login can read these tables directly.

ALTER TABLE public.complaint_messages
    ADD COLUMN delivery_status text NOT NULL DEFAULT 'sent',
    ADD COLUMN delivered_at timestamptz,
    ADD COLUMN read_at timestamptz,
    ADD COLUMN sender_external text;
ALTER TABLE public.complaint_messages
    ADD CONSTRAINT ck_message_delivery_status
    CHECK (delivery_status IN ('queued', 'sent', 'delivered', 'read', 'received', 'failed'));
ALTER TABLE public.customers
    ADD COLUMN voice_followup_opt_in boolean NOT NULL DEFAULT false,
    ADD COLUMN voice_consent_at timestamptz;
ALTER TABLE public.call_logs ALTER COLUMN started_at DROP NOT NULL;
ALTER TABLE public.call_logs
    ADD COLUMN sender_staff_id uuid REFERENCES public.staff_users(id),
    ADD COLUMN scheduled_at timestamptz,
    ADD COLUMN status text NOT NULL DEFAULT 'completed',
    ADD COLUMN provider text;
ALTER TABLE public.call_logs
    ADD CONSTRAINT ck_call_log_status
    CHECK (status IN ('scheduled', 'queued', 'ringing', 'in_progress', 'completed', 'failed', 'human_fallback'));
CREATE UNIQUE INDEX ux_call_logs_provider_call_id
    ON public.call_logs(provider,provider_call_id) WHERE provider_call_id IS NOT NULL;

ALTER TABLE public.ai_insights DROP CONSTRAINT ck_ai_kind;
ALTER TABLE public.ai_insights
    ADD CONSTRAINT ck_ai_kind
    CHECK (kind IN ('classification', 'sentiment', 'summary', 'reply', 'similar'));
DROP POLICY ai_insights_read ON public.ai_insights;
CREATE POLICY ai_insights_read ON public.ai_insights FOR SELECT TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND app_security.has_permission('integrations.ai_call')
        AND app_security.can_read_complaint_id(complaint_id, complaint_created_at));

ALTER TABLE public.integrations_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integrations_config FORCE ROW LEVEL SECURITY;
ALTER TABLE public.webhooks
    ADD COLUMN signing_secret_ciphertext bytea;
ALTER TABLE public.webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.webhooks FORCE ROW LEVEL SECURITY;
ALTER TABLE public.api_keys
    ADD COLUMN all_companies boolean NOT NULL DEFAULT false,
    ADD COLUMN company_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[];
-- Legacy keys did not carry a company boundary, so revoke them instead of
-- silently widening their authority during the migration.
UPDATE public.api_keys SET revoked_at = now() WHERE revoked_at IS NULL;
ALTER TABLE public.api_keys
    ADD CONSTRAINT ck_api_key_company_scope
    CHECK ((all_companies AND cardinality(company_ids) = 0)
        OR (NOT all_companies AND cardinality(company_ids) BETWEEN 1 AND 100)
        OR (revoked_at IS NOT NULL AND NOT all_companies AND cardinality(company_ids) = 0));
UPDATE public.webhooks SET is_active = false WHERE signing_secret_ciphertext IS NULL;
ALTER TABLE public.api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.api_keys FORCE ROW LEVEL SECURITY;

CREATE TABLE public.integration_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_kind text NOT NULL,
    provider text NOT NULL,
    company_id uuid REFERENCES public.insurance_companies(id),
    complaint_id uuid,
    complaint_created_at timestamptz,
    sender_staff_id uuid REFERENCES public.staff_users(id),
    integration_config_id uuid REFERENCES public.integrations_config(id),
    webhook_id uuid REFERENCES public.webhooks(id),
    message_id uuid REFERENCES public.complaint_messages(id),
    call_log_id uuid REFERENCES public.call_logs(id),
    payload_ciphertext bytea NOT NULL,
    idempotency_digest bytea NOT NULL,
    payload_digest bytea NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 5,
    available_at timestamptz NOT NULL DEFAULT now(),
    locked_until timestamptz,
    last_error_code text,
    provider_message_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES public.complaints(id, created_at) ON DELETE CASCADE,
    CONSTRAINT ck_integration_job_kind CHECK (job_kind IN (
        'send_email', 'send_whatsapp', 'ai_analyze', 'voice_followup',
        'webhook_send', 'inbound_email', 'inbound_whatsapp',
        'whatsapp_status', 'email_status', 'inbound_voice', 'voice_status'
    )),
    CONSTRAINT ck_integration_job_state CHECK (state IN (
        'queued', 'processing', 'sent', 'failed', 'dead_letter'
    )),
    CONSTRAINT ck_integration_job_attempts CHECK (
        attempts BETWEEN 0 AND max_attempts AND max_attempts BETWEEN 1 AND 10
    ),
    CONSTRAINT ck_integration_job_digests CHECK (
        octet_length(idempotency_digest) = 32 AND octet_length(payload_digest) = 32
    ),
    CONSTRAINT ck_integration_job_parent CHECK (
        (complaint_id IS NULL AND complaint_created_at IS NULL)
        OR (complaint_id IS NOT NULL AND complaint_created_at IS NOT NULL)
    ),
    CONSTRAINT ck_integration_job_sender CHECK (
        (job_kind LIKE 'inbound_%' OR job_kind IN ('whatsapp_status','email_status','voice_status'))
        OR sender_staff_id IS NOT NULL
    )
);
CREATE UNIQUE INDEX ux_integration_job_idempotency
    ON public.integration_jobs(idempotency_digest);
CREATE INDEX ix_integration_job_claim
    ON public.integration_jobs(state, available_at, created_at)
    WHERE state IN ('queued', 'processing');
CREATE INDEX ix_integration_job_sender
    ON public.integration_jobs(sender_staff_id, created_at DESC);
CREATE INDEX ix_integration_job_complaint
    ON public.integration_jobs(complaint_id, complaint_created_at, created_at DESC)
    WHERE complaint_id IS NOT NULL;
ALTER TABLE public.ai_insights
    ADD COLUMN integration_job_id uuid REFERENCES public.integration_jobs(id);

CREATE TABLE public.integration_dead_letters (
    job_id uuid PRIMARY KEY REFERENCES public.integration_jobs(id) ON DELETE CASCADE,
    job_kind text NOT NULL,
    error_code text NOT NULL,
    attempts integer NOT NULL,
    payload_ciphertext bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    replayed_at timestamptz,
    replayed_by_staff_id uuid REFERENCES public.staff_users(id),
    CONSTRAINT ck_dead_letter_error CHECK (length(error_code) BETWEEN 1 AND 80)
);
CREATE INDEX ix_integration_dead_letters_created
    ON public.integration_dead_letters(created_at DESC) WHERE replayed_at IS NULL;

ALTER TABLE public.integration_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integration_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE public.integration_dead_letters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.integration_dead_letters FORCE ROW LEVEL SECURITY;

-- Secret-bearing and queue tables are reachable only through the functions below.
REVOKE ALL ON public.integrations_config, public.webhooks, public.api_keys,
    public.integration_jobs, public.integration_dead_letters FROM app_runtime, app_worker;
REVOKE ALL ON public.integration_jobs, public.integration_dead_letters FROM PUBLIC;

-- A message's channel-specific send grant is independent of portal reply grants.
DROP POLICY complaint_messages_insert ON public.complaint_messages;
CREATE POLICY complaint_messages_insert ON public.complaint_messages FOR INSERT TO app_runtime
    WITH CHECK (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
        AND ((app_security.actor_scope() = 'staff'
              AND sender_staff_id = app_security.actor_id()
              AND direction = 'outbound'
              AND ((visibility = 'public' AND
                    ((channel = 'email' AND app_security.has_permission('integrations.send_email'))
                  OR (channel = 'whatsapp'
                      AND app_security.has_permission('integrations.send_whatsapp'))
                  OR (channel NOT IN ('email', 'whatsapp')
                      AND app_security.has_permission('complaints.reply_public'))))
                OR (visibility = 'internal' AND channel NOT IN ('email','whatsapp')
                    AND app_security.has_permission('complaints.add_internal_note'))))
          OR (app_security.actor_scope() = 'customer' AND visibility = 'public'
              AND direction = 'inbound' AND sender_customer_id = app_security.actor_id())));

CREATE FUNCTION app_security.integration_company_allowed(p_company uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.actor_scope() = 'staff'
       AND app_security.has_permission('integrations.manage')
       AND NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                       WHERE t.user_id = app_security.actor_id())
       AND CASE WHEN p_company IS NULL THEN app_security.has_global_scope()
           ELSE app_security.has_company_scope(p_company) END
$function$;

CREATE FUNCTION app_security.save_integration_config(
    p_company uuid, p_provider text, p_ciphertext bytea, p_enabled boolean,
    p_clear_credentials boolean DEFAULT false)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_old_enabled boolean;
BEGIN
    IF NOT app_security.integration_company_allowed(p_company)
       OR p_provider NOT IN ('smtp','sendgrid','ses','whatsapp_cloud','email_webhook','imap',
                             'mock_llm','openai','mock_voice','twilio','vonage')
       OR (p_provider IN ('email_webhook','imap') AND p_company IS NULL)
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
        secret_ref, is_enabled, updated_at)
        VALUES (p_company,p_provider,CASE WHEN p_clear_credentials THEN NULL ELSE p_ciphertext END,
            CASE WHEN p_clear_credentials THEN NULL ELSE 'encrypted-at-rest' END,p_enabled,now())
        ON CONFLICT (company_id, provider) DO UPDATE SET
            config_ciphertext=CASE WHEN p_clear_credentials THEN NULL ELSE COALESCE(
                EXCLUDED.config_ciphertext,public.integrations_config.config_ciphertext) END,
            secret_ref=CASE WHEN p_clear_credentials THEN NULL ELSE 'encrypted-at-rest' END,
            is_enabled=EXCLUDED.is_enabled,
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

CREATE FUNCTION app_security.list_integration_configs()
RETURNS TABLE (id uuid, company_id uuid, provider text, is_enabled boolean,
               has_credentials boolean, updated_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'staff'
       OR NOT app_security.has_permission('integrations.manage') THEN RETURN; END IF;
    RETURN QUERY SELECT i.id, i.company_id, i.provider, i.is_enabled,
        i.config_ciphertext IS NOT NULL, i.updated_at
      FROM public.integrations_config i
      WHERE (i.company_id IS NULL AND app_security.has_global_scope())
         OR (i.company_id IS NOT NULL AND app_security.integration_company_allowed(i.company_id))
      ORDER BY i.company_id NULLS FIRST, i.provider;
END;
$function$;

-- Public provider callbacks only receive an encrypted config for the exact
-- company/provider pair (or a global fallback) after a purpose-scoped request.
CREATE FUNCTION app_security.webhook_integration_config(p_company uuid, p_provider text)
RETURNS TABLE (config_company_id uuid, config_ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_cipher bytea; v_enabled boolean; v_scope uuid;
BEGIN
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'webhook'
       OR p_provider NOT IN ('email_webhook','whatsapp_cloud','mock_voice','twilio','vonage')
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

CREATE FUNCTION app_security.enqueue_inbound_job(
    p_company uuid, p_provider text, p_job_kind text, p_ciphertext bytea,
    p_idempotency bytea, p_payload_digest bytea)
RETURNS TABLE (job_id uuid, created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_created boolean := false;
BEGIN
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'webhook'
       OR p_job_kind NOT IN ('inbound_email','inbound_whatsapp','whatsapp_status',
                             'email_status','inbound_voice','voice_status')
       OR p_provider NOT IN ('email_webhook','whatsapp_cloud','mock_voice','twilio','vonage', 'imap')
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

CREATE FUNCTION app_security.create_integration_job(
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
    INSERT INTO public.integration_jobs(job_kind,provider,company_id,complaint_id,
        complaint_created_at,sender_staff_id,integration_config_id,message_id,call_log_id,
        payload_ciphertext,idempotency_digest,payload_digest,available_at)
      VALUES(p_job_kind,v_config.provider,p_company,p_complaint,p_created,p_sender,
        v_config.id,p_message,p_call,p_ciphertext,p_idempotency,p_payload_digest,
        COALESCE(p_scheduled_at,now()))
      ON CONFLICT (idempotency_digest) DO NOTHING RETURNING id,state INTO v_id,v_state;
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

CREATE FUNCTION app_security.integration_job_status(p_id uuid)
RETURNS TABLE (id uuid, job_kind text, state text, attempts integer,
               last_error_code text, created_at timestamptz, finished_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT j.id, j.job_kind, j.state, j.attempts, j.last_error_code, j.created_at, j.finished_at
    FROM public.integration_jobs j
    WHERE j.id=p_id AND app_security.actor_scope()='staff'
      AND j.sender_staff_id=app_security.actor_id()
      AND CASE j.job_kind
        WHEN 'send_email' THEN app_security.has_permission('integrations.send_email')
        WHEN 'send_whatsapp' THEN app_security.has_permission('integrations.send_whatsapp')
        WHEN 'ai_analyze' THEN app_security.has_permission('integrations.ai_call')
        WHEN 'voice_followup' THEN app_security.has_permission('integrations.ai_call')
        ELSE app_security.has_permission('integrations.manage') END
      AND (j.complaint_id IS NULL OR
        app_security.can_read_complaint_id(j.complaint_id,j.complaint_created_at))
$function$;

CREATE FUNCTION app_security.save_webhook(
    p_id uuid, p_company uuid, p_url text, p_events text[], p_ciphertext bytea, p_active boolean)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_created boolean:=false;
BEGIN
    IF NOT app_security.integration_company_allowed(p_company)
       OR p_url !~ '^https://[^/@[:space:]]+(/[^[:space:]]*)?$'
       OR length(p_url) > 2048 OR p_events IS NULL OR cardinality(p_events) NOT BETWEEN 1 AND 10
       OR EXISTS (SELECT 1 FROM unnest(p_events) e
           WHERE e NOT IN ('complaint.status_changed','complaint.message_received'))
       OR (p_ciphertext IS NOT NULL AND octet_length(p_ciphertext) < 28)
       OR (p_active AND p_ciphertext IS NULL AND (p_id IS NULL OR NOT EXISTS (
          SELECT 1 FROM public.webhooks w WHERE w.id=p_id
            AND w.signing_secret_ciphertext IS NOT NULL))) THEN
        RAISE EXCEPTION 'webhook configuration forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NULL OR NOT EXISTS (SELECT 1 FROM public.webhooks w WHERE w.id=p_id) THEN
        INSERT INTO public.webhooks(id,company_id,url,event_types,signing_secret_ref,
            signing_secret_ciphertext,is_active)
          VALUES (COALESCE(p_id,gen_random_uuid()),p_company,p_url,p_events,
            'encrypted-at-rest',p_ciphertext,p_active)
          RETURNING id INTO v_id;
        v_created:=true;
    ELSE
        UPDATE public.webhooks w SET url=p_url,event_types=p_events,
            signing_secret_ciphertext=COALESCE(p_ciphertext,w.signing_secret_ciphertext),
            signing_secret_ref='encrypted-at-rest',is_active=p_active
          WHERE w.id=p_id AND w.company_id IS NOT DISTINCT FROM p_company
            AND w.signing_secret_ciphertext IS NOT NULL
          RETURNING id INTO v_id;
    END IF;
    IF v_id IS NULL THEN RETURN NULL; END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
      VALUES (app_security.actor_id(),CASE WHEN v_created THEN 'webhook_created'
        ELSE 'webhook_updated' END,'webhook',v_id,
        jsonb_build_object('company_id',p_company,'events',to_jsonb(p_events),'active',p_active));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.list_webhooks()
RETURNS TABLE (id uuid, company_id uuid, url text, event_types text[], is_active boolean,
               last_delivery_at timestamptz, created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'staff'
       OR NOT app_security.has_permission('integrations.manage') THEN RETURN; END IF;
    RETURN QUERY SELECT w.id,w.company_id,w.url,w.event_types,w.is_active,
        w.last_delivery_at,w.created_at FROM public.webhooks w
      WHERE (w.company_id IS NULL AND app_security.has_global_scope())
         OR (w.company_id IS NOT NULL AND app_security.integration_company_allowed(w.company_id))
      ORDER BY w.created_at DESC LIMIT 200;
END;
$function$;


CREATE FUNCTION app_security.enqueue_webhook_job(
    p_webhook uuid,p_complaint uuid,p_created timestamptz,p_company uuid,p_event text,
    p_ciphertext bytea,p_idempotency bytea,p_payload_digest bytea)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_digest bytea;
BEGIN
    IF session_user <> 'app_runtime' OR app_security.actor_scope() <> 'staff'
       OR NOT app_security.can_read_complaint_id(p_complaint,p_created)
       OR octet_length(p_ciphertext)<28 OR octet_length(p_idempotency)<>32
       OR octet_length(p_payload_digest)<>32 THEN
        RAISE EXCEPTION 'webhook enqueue forbidden' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.webhooks w JOIN public.complaints c
       ON c.id=p_complaint AND c.created_at=p_created
       WHERE w.id=p_webhook AND w.is_active AND w.signing_secret_ciphertext IS NOT NULL
         AND p_event=ANY(w.event_types) AND (w.company_id IS NULL OR w.company_id=p_company)
         AND c.company_id=p_company) THEN RETURN NULL; END IF;
    INSERT INTO public.integration_jobs(job_kind,provider,company_id,complaint_id,
       complaint_created_at,sender_staff_id,webhook_id,payload_ciphertext,
       idempotency_digest,payload_digest)
      VALUES ('webhook_send','hmac',p_company,p_complaint,p_created,app_security.actor_id(),
        p_webhook,p_ciphertext,p_idempotency,p_payload_digest)
      ON CONFLICT (idempotency_digest) DO NOTHING RETURNING id INTO v_id;
    IF v_id IS NULL THEN
        SELECT j.id,j.payload_digest INTO v_id,v_digest FROM public.integration_jobs j
            WHERE j.idempotency_digest=p_idempotency;
        IF v_digest IS DISTINCT FROM p_payload_digest THEN
            RAISE EXCEPTION 'idempotency key reused with different payload' USING ERRCODE = '23505';
        END IF;
    ELSE
        INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,
            complaint_id,complaint_created_at,after_state)
          VALUES (app_security.actor_id(),'webhook_queued','webhook',p_webhook,p_complaint,
            p_created,jsonb_build_object('job_id',v_id));
    END IF;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.create_api_key(
    p_prefix text,p_hash bytea,p_codes text[],p_all boolean,p_companies uuid[],p_expires timestamptz)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'staff'
       OR NOT app_security.has_permission('integrations.manage')
       OR p_prefix !~ '^ipk_[A-Za-z0-9_-]{12,24}$'
       OR octet_length(p_hash)<>32 OR p_codes IS NULL OR cardinality(p_codes)>40
       OR EXISTS (SELECT 1 FROM unnest(p_codes) p WHERE NOT app_security.has_permission(p))
       OR p_companies IS NULL
       OR (p_all AND (cardinality(p_companies)<>0 OR NOT app_security.has_global_scope()))
       OR (NOT p_all AND (cardinality(p_companies) NOT BETWEEN 1 AND 100
           OR EXISTS (SELECT 1 FROM unnest(p_companies) c
                WHERE NOT app_security.has_company_scope(c))))
       OR (p_expires IS NOT NULL AND (p_expires<=now() OR p_expires>now()+interval '366 days')) THEN
        RAISE EXCEPTION 'API key creation forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.api_keys(owner_staff_id,key_prefix,key_hash,permission_codes,
        all_companies,company_ids,expires_at)
      VALUES(app_security.actor_id(),p_prefix,p_hash,p_codes,p_all,p_companies,p_expires)
      RETURNING id INTO v_id;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
      VALUES(app_security.actor_id(),'api_key_created','api_key',v_id,
        jsonb_build_object('permissions',to_jsonb(p_codes),'all_companies',p_all,
                           'company_ids',to_jsonb(p_companies),'expires_at',p_expires));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.list_api_keys()
RETURNS TABLE (id uuid,key_prefix text,permission_codes text[],all_companies boolean,
               company_ids uuid[],expires_at timestamptz,revoked_at timestamptz,
               last_used_at timestamptz,created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope()<>'staff' OR NOT app_security.has_permission('integrations.manage')
       THEN RETURN; END IF;
    RETURN QUERY SELECT k.id,k.key_prefix,k.permission_codes,k.all_companies,k.company_ids,
       k.expires_at,k.revoked_at,k.last_used_at,k.created_at FROM public.api_keys k
       WHERE k.owner_staff_id=app_security.actor_id() ORDER BY k.created_at DESC LIMIT 100;
END;
$function$;

CREATE FUNCTION app_security.revoke_api_key(p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope()<>'staff' OR NOT app_security.has_permission('integrations.manage')
       THEN RETURN false; END IF;
    UPDATE public.api_keys SET revoked_at=COALESCE(revoked_at,now())
       WHERE id=p_id AND owner_staff_id=app_security.actor_id();
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id)
       VALUES(app_security.actor_id(),'api_key_revoked','api_key',p_id);
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.api_key_identity(p_prefix text,p_hash bytea)
RETURNS TABLE (owner_staff_id uuid,permission_codes text[],all_companies boolean,company_ids uuid[])
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user<>'app_runtime' OR app_security.actor_scope()<>'guest' THEN RETURN; END IF;
    RETURN QUERY UPDATE public.api_keys k SET last_used_at=now()
      FROM public.staff_users s WHERE k.key_prefix=p_prefix AND k.key_hash=p_hash
        AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at>now())
        AND s.id=k.owner_staff_id AND s.status='active'
      RETURNING k.owner_staff_id,k.permission_codes,k.all_companies,k.company_ids;
END;
$function$;

-- The API-key query runs as the non-BYPASSRLS caller after validating both the
-- key's own grants and the owner's current role/scope; no key bypasses revocation.
CREATE FUNCTION app_security.api_key_ticket(p_prefix text,p_hash bytea,p_ticket text)
RETURNS TABLE (id uuid,ticket_no text,company_id uuid,insurance_type text,status text,
               priority text,subject text,created_at timestamptz,updated_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_key public.api_keys%ROWTYPE; v_old_scope text:=current_setting('app.scope',true);
    v_old_id text:=current_setting('app.user_id',true);
BEGIN
    SELECT k.* INTO v_key FROM public.api_keys k
       JOIN public.staff_users s ON s.id=k.owner_staff_id
       WHERE k.key_prefix=p_prefix AND k.key_hash=p_hash AND k.revoked_at IS NULL
         AND (k.expires_at IS NULL OR k.expires_at>now()) AND s.status='active';
    IF NOT FOUND OR NOT ('complaints.view'=ANY(v_key.permission_codes)) THEN RETURN; END IF;
    PERFORM set_config('app.scope','staff',true);
    PERFORM set_config('app.user_id',v_key.owner_staff_id::text,true);
    IF NOT app_security.has_permission('complaints.view') THEN
        PERFORM set_config('app.scope',COALESCE(v_old_scope,''),true);
        PERFORM set_config('app.user_id',COALESCE(v_old_id,''),true);
        RETURN;
    END IF;
    UPDATE public.api_keys SET last_used_at=now() WHERE id=v_key.id;
    RETURN QUERY SELECT c.id,c.ticket_no,c.company_id,c.insurance_type::text,c.status::text,
        c.priority::text,c.subject,c.created_at,c.updated_at FROM public.complaints c
      WHERE c.ticket_no=p_ticket
        AND (v_key.all_companies OR c.company_id=ANY(v_key.company_ids))
        AND app_security.can_read_complaint(c.company_id,c.insurance_type,
            c.assigned_agent_id,c.team_id,c.customer_id);
    PERFORM set_config('app.scope',COALESCE(v_old_scope,''),true);
    PERFORM set_config('app.user_id',COALESCE(v_old_id,''),true);
END;
$function$;

CREATE FUNCTION app_security.create_call_log(
    p_id uuid,p_complaint uuid,p_created timestamptz,p_sender uuid,p_scheduled timestamptz)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope()<>'staff' OR p_sender<>app_security.actor_id()
       OR NOT app_security.has_permission('integrations.ai_call')
       OR NOT app_security.has_permission('complaints.view')
       OR NOT app_security.can_read_complaint_id(p_complaint,p_created) THEN
        RAISE EXCEPTION 'voice consent or scope required' USING ERRCODE = '42501';
    END IF;
    SELECT c.id INTO v_id FROM public.call_logs c WHERE c.id=p_id
       AND c.complaint_id=p_complaint AND c.complaint_created_at=p_created
       AND c.sender_staff_id=p_sender;
    IF FOUND THEN RETURN v_id; END IF;
    IF p_scheduled<now()-interval '1 minute' OR p_scheduled>now()+interval '90 days' THEN
        RAISE EXCEPTION 'voice schedule invalid' USING ERRCODE='22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.complaints c JOIN public.customers u
        ON u.id=c.customer_id WHERE c.id=p_complaint AND c.created_at=p_created
          AND u.voice_followup_opt_in AND u.voice_consent_at IS NOT NULL) THEN
        RAISE EXCEPTION 'customer voice consent is required' USING ERRCODE='42501';
    END IF;
    INSERT INTO public.call_logs(id,complaint_id,complaint_created_at,direction,
        sender_staff_id,consent_at,scheduled_at,status,started_at)
      SELECT p_id,p_complaint,p_created,'outbound',p_sender,u.voice_consent_at,
          p_scheduled,'scheduled',NULL FROM public.complaints c
        JOIN public.customers u ON u.id=c.customer_id
        WHERE c.id=p_complaint AND c.created_at=p_created
      ON CONFLICT(id) DO NOTHING RETURNING id INTO v_id;
    IF v_id IS NULL THEN
        SELECT c.id INTO v_id FROM public.call_logs c WHERE c.id=p_id
           AND c.complaint_id=p_complaint AND c.complaint_created_at=p_created
           AND c.sender_staff_id=p_sender;
        IF v_id IS NULL THEN RAISE EXCEPTION 'call idempotency conflict' USING ERRCODE='23505'; END IF;
        RETURN v_id;
    END IF;
    INSERT INTO public.complaint_events(complaint_id,complaint_created_at,event_type,
        actor_staff_id,metadata)
      VALUES(p_complaint,p_created,'voice_followup_scheduled',p_sender,
        jsonb_build_object('call_log_id',v_id,'scheduled_at',p_scheduled));
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,
        complaint_id,complaint_created_at,after_state)
      VALUES(p_sender,'voice_followup_scheduled','call_log',v_id,p_complaint,p_created,
        jsonb_build_object('consent_recorded',true,'consent_at',
            (SELECT voice_consent_at FROM public.customers WHERE id=(SELECT customer_id
              FROM public.complaints WHERE id=p_complaint AND created_at=p_created)),
            'scheduled_at',p_scheduled));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.company_contact_emails(p_company uuid)
RETURNS text[] LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_emails text[];
BEGIN
    IF app_security.actor_scope()<>'staff'
       OR NOT app_security.has_permission('integrations.send_email')
       OR NOT app_security.has_permission('complaints.view')
       OR NOT app_security.has_company_scope(p_company) THEN RETURN NULL; END IF;
    SELECT contact_emails INTO v_emails FROM public.insurance_companies
      WHERE id=p_company AND is_active;
    RETURN v_emails;
END;
$function$;

CREATE FUNCTION app_security.webhook_delivery_secrets(p_id uuid)
RETURNS TABLE (url text, ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker' THEN RETURN; END IF;
    RETURN QUERY SELECT w.url,w.signing_secret_ciphertext FROM public.webhooks w
      WHERE w.id=p_id AND w.is_active AND w.signing_secret_ciphertext IS NOT NULL;
END;
$function$;

CREATE FUNCTION app_security.webhooks_for_event(p_complaint uuid,p_created timestamptz,p_event text)
RETURNS TABLE (id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_company uuid;
BEGIN
    IF app_security.actor_scope()<>'staff' OR NOT app_security.has_permission('complaints.view')
       OR NOT app_security.can_read_complaint_id(p_complaint,p_created) THEN RETURN; END IF;
    SELECT c.company_id INTO v_company FROM public.complaints c
      WHERE c.id=p_complaint AND c.created_at=p_created;
    RETURN QUERY SELECT w.id FROM public.webhooks w WHERE w.is_active
       AND p_event=ANY(w.event_types) AND (w.company_id IS NULL OR w.company_id=v_company)
       AND w.signing_secret_ciphertext IS NOT NULL;
END;
$function$;

CREATE FUNCTION app_security.claim_integration_jobs(p_limit integer DEFAULT 20)
RETURNS TABLE (job_id uuid,job_kind text,provider text,company_id uuid,complaint_id uuid,
    complaint_created_at timestamptz,sender_staff_id uuid,message_id uuid,call_log_id uuid,
    idempotency_digest bytea,payload_ciphertext bytea,config_ciphertext bytea,
    config_company_id uuid,webhook_id uuid,webhook_url text,
    webhook_secret_ciphertext bytea,attempt integer,recipient_mobile text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE; v_allowed boolean; v_error text;
    v_prior_scope text:=current_setting('app.scope',true);
    v_prior_id text:=current_setting('app.user_id',true);
    v_config bytea; v_config_company uuid; v_url text; v_webhook_cipher bytea;
    v_permission text; v_mobile text; v_attempt integer;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    FOR j IN SELECT q.* FROM public.integration_jobs q
      WHERE ((q.state='queued' AND q.available_at<=now())
          OR (q.state='processing' AND q.locked_until<=now()))
      ORDER BY q.available_at,q.created_at,q.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        v_allowed:=true; v_error:=NULL; v_config:=NULL; v_config_company:=NULL;
        v_url:=NULL; v_webhook_cipher:=NULL; v_mobile:=NULL;
        IF j.attempts>=j.max_attempts THEN
            v_allowed:=false; v_error:='retry_limit_exceeded';
        ELSIF j.sender_staff_id IS NOT NULL THEN
            PERFORM set_config('app.scope','staff',true);
            PERFORM set_config('app.user_id',j.sender_staff_id::text,true);
            v_permission:=CASE j.job_kind
                WHEN 'send_email' THEN 'integrations.send_email'
                WHEN 'send_whatsapp' THEN 'integrations.send_whatsapp'
                WHEN 'ai_analyze' THEN 'integrations.ai_call'
                WHEN 'voice_followup' THEN 'integrations.ai_call'
                ELSE 'complaints.view' END;
            IF NOT app_security.is_active_staff()
               OR NOT app_security.has_permission(v_permission)
               OR (j.complaint_id IS NOT NULL AND
                   NOT app_security.can_read_complaint_id(j.complaint_id,j.complaint_created_at)) THEN
                v_allowed:=false; v_error:='authorization_revoked';
            ELSIF j.job_kind='send_whatsapp' THEN
                SELECT u.mobile INTO v_mobile FROM public.complaints c
                    JOIN public.customers u ON u.id=c.customer_id
                    WHERE c.id=j.complaint_id AND c.created_at=j.complaint_created_at
                      AND u.whatsapp_available;
                IF NOT FOUND OR v_mobile IS NULL THEN
                    v_allowed:=false; v_error:='whatsapp_opt_out';
                END IF;
            ELSIF j.job_kind='voice_followup' THEN
                SELECT u.mobile INTO v_mobile FROM public.call_logs l
                    JOIN public.complaints c ON c.id=l.complaint_id
                        AND c.created_at=l.complaint_created_at
                    JOIN public.customers u ON u.id=c.customer_id
                    WHERE l.id=j.call_log_id AND u.voice_followup_opt_in
                        AND u.voice_consent_at=l.consent_at;
                IF NOT FOUND OR v_mobile IS NULL THEN
                    v_allowed:=false; v_error:='consent_revoked';
                END IF;
            END IF;
            PERFORM set_config('app.scope',COALESCE(v_prior_scope,''),true);
            PERFORM set_config('app.user_id',COALESCE(v_prior_id,''),true);
        END IF;
        IF j.integration_config_id IS NOT NULL THEN
            SELECT i.config_ciphertext,i.company_id INTO v_config,v_config_company
              FROM public.integrations_config i
              WHERE i.id=j.integration_config_id AND i.is_enabled
                AND i.config_ciphertext IS NOT NULL;
            IF v_config IS NULL THEN v_allowed:=false; v_error:='integration_disconnected'; END IF;
        END IF;
        IF j.webhook_id IS NOT NULL THEN
            SELECT w.url,w.signing_secret_ciphertext INTO v_url,v_webhook_cipher
              FROM public.webhooks w WHERE w.id=j.webhook_id AND w.is_active;
            IF v_url IS NULL OR v_webhook_cipher IS NULL THEN
                v_allowed:=false; v_error:='webhook_disconnected';
            END IF;
        END IF;
        IF NOT v_allowed THEN
            UPDATE public.integration_jobs SET state='dead_letter',last_error_code=v_error,
                finished_at=now(),locked_until=NULL WHERE id=j.id;
            INSERT INTO public.integration_dead_letters(job_id,job_kind,error_code,attempts,
                payload_ciphertext) VALUES(j.id,j.job_kind,v_error,j.attempts,j.payload_ciphertext)
                ON CONFLICT(job_id) DO UPDATE SET error_code=EXCLUDED.error_code,
                attempts=EXCLUDED.attempts,created_at=now(),replayed_at=NULL;
            CONTINUE;
        END IF;
        UPDATE public.integration_jobs SET state='processing',attempts=attempts+1,
            locked_until=now()+interval '5 minutes' WHERE id=j.id
            RETURNING attempts INTO v_attempt;
        RETURN QUERY SELECT j.id,j.job_kind,j.provider,j.company_id,j.complaint_id,
            j.complaint_created_at,j.sender_staff_id,j.message_id,j.call_log_id,
            j.idempotency_digest,j.payload_ciphertext,v_config,v_config_company,
            j.webhook_id,v_url,v_webhook_cipher,v_attempt,v_mobile;
    END LOOP;
END;
$function$;

CREATE FUNCTION app_security.clean_job_attachments(
    p_job uuid,p_attempt integer,p_attachment_ids uuid[])
RETURNS TABLE (attachment_id uuid,storage_key text,filename text,mime_type text,
               size_bytes bigint,checksum_sha256 text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_complaint uuid; v_created timestamptz; v_count integer;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_attachment_ids IS NULL OR cardinality(p_attachment_ids)>5 THEN RETURN; END IF;
    SELECT j.complaint_id,j.complaint_created_at INTO v_complaint,v_created
       FROM public.integration_jobs j WHERE j.id=p_job AND j.state='processing'
         AND j.attempts=p_attempt AND j.job_kind='send_email';
    IF v_complaint IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO v_count FROM public.attachments a
      WHERE a.complaint_id=v_complaint AND a.complaint_created_at=v_created
        AND a.scan_status='clean' AND a.id=ANY(p_attachment_ids);
    IF v_count<>cardinality(p_attachment_ids) THEN
        RAISE EXCEPTION 'outbound attachment is not clean or not on ticket' USING ERRCODE='42501';
    END IF;
    RETURN QUERY SELECT a.id,a.storage_key,a.original_filename,a.mime_type,a.size_bytes,
        a.checksum_sha256 FROM public.attachments a
      WHERE a.complaint_id=v_complaint AND a.complaint_created_at=v_created
        AND a.scan_status='clean' AND a.id=ANY(p_attachment_ids)
      ORDER BY array_position(p_attachment_ids,a.id);
END;
$function$;

CREATE FUNCTION app_security.finish_integration_job(
    p_id uuid,p_attempt integer,p_state text,p_error_code text DEFAULT NULL,
    p_provider_id text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE; v_dead boolean:=false; v_state text;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_state NOT IN ('sent','retry','permanent')
       OR (p_error_code IS NOT NULL AND p_error_code !~ '^[a-z0-9_:-]{1,80}$') THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id=p_id
       AND q.state='processing' AND q.attempts=p_attempt FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_state='sent' THEN
        UPDATE public.integration_jobs SET state='sent',provider_message_id=p_provider_id,
            last_error_code=NULL,finished_at=now(),locked_until=NULL WHERE id=p_id;
        IF j.message_id IS NOT NULL THEN
            UPDATE public.complaint_messages SET delivery_status='sent',
                external_message_id=COALESCE(p_provider_id,external_message_id)
              WHERE id=j.message_id;
        END IF;
        IF j.webhook_id IS NOT NULL THEN
            UPDATE public.webhooks SET last_delivery_at=now() WHERE id=j.webhook_id;
        END IF;
        RETURN true;
    END IF;
    v_dead:=p_state='permanent' OR j.attempts>=j.max_attempts;
    v_state:=CASE WHEN v_dead THEN 'dead_letter' ELSE 'queued' END;
    UPDATE public.integration_jobs SET state=v_state,last_error_code=COALESCE(p_error_code,'provider_error'),
        available_at=CASE WHEN v_dead THEN available_at ELSE
            now()+make_interval(secs=>LEAST(3600,(30*(2^LEAST(attempts,7)))::integer)) END,
        finished_at=CASE WHEN v_dead THEN now() ELSE NULL END,locked_until=NULL WHERE id=p_id;
    IF j.message_id IS NOT NULL THEN
        UPDATE public.complaint_messages SET delivery_status=CASE WHEN v_dead THEN 'failed' ELSE 'queued' END
          WHERE id=j.message_id;
    END IF;
    IF v_dead THEN
        INSERT INTO public.integration_dead_letters(job_id,job_kind,error_code,attempts,payload_ciphertext)
          VALUES(j.id,j.job_kind,COALESCE(p_error_code,'provider_error'),j.attempts,j.payload_ciphertext)
          ON CONFLICT(job_id) DO UPDATE SET error_code=EXCLUDED.error_code,
             attempts=EXCLUDED.attempts,created_at=now(),replayed_at=NULL;
    END IF;
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.complete_ai_job(p_id uuid,p_attempt integer,p_items jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE; item jsonb; v_kind text; v_cipher bytea;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_items IS NULL OR jsonb_typeof(p_items) IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;
    IF jsonb_array_length(p_items) NOT BETWEEN 1 AND 10 THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id=p_id AND q.job_kind='ai_analyze'
       AND q.state='processing' AND q.attempts=p_attempt FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    FOR item IN SELECT value FROM jsonb_array_elements(p_items)
    LOOP
        v_kind:=item->>'kind';
        IF v_kind NOT IN ('classification','sentiment','summary','reply','similar')
           OR item->>'ciphertext' IS NULL OR item->>'confidence' IS NULL THEN
            RAISE EXCEPTION 'invalid encrypted AI result' USING ERRCODE='22023';
        END IF;
        v_cipher:=decode(item->>'ciphertext','base64');
        IF octet_length(v_cipher)<28 THEN RAISE EXCEPTION 'invalid AI ciphertext' USING ERRCODE='22023'; END IF;
        INSERT INTO public.ai_insights(complaint_id,complaint_created_at,kind,payload_ciphertext,
            confidence,provider_model,integration_job_id,review_status)
          VALUES(j.complaint_id,j.complaint_created_at,v_kind,v_cipher,
            (item->>'confidence')::numeric,left(item->>'provider_model',120),j.id,'suggested');
    END LOOP;
    UPDATE public.integration_jobs SET state='sent',finished_at=now(),locked_until=NULL,
        last_error_code=NULL WHERE id=p_id;
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.whatsapp_session_open(p_id uuid,p_attempt integer)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT session_user='app_worker' AND app_security.actor_scope()='worker'
       AND EXISTS (SELECT 1 FROM public.integration_jobs j
         JOIN public.complaint_messages m ON m.complaint_id=j.complaint_id
            AND m.complaint_created_at=j.complaint_created_at
         WHERE j.id=p_id AND j.job_kind='send_whatsapp' AND j.state='processing'
           AND j.attempts=p_attempt AND m.channel='whatsapp' AND m.direction='inbound'
           AND m.created_at>=now()-interval '24 hours')
$function$;

CREATE FUNCTION app_security.review_ai_insight(
    p_complaint uuid,p_created timestamptz,p_insight uuid,p_accept boolean)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope()<>'staff' OR NOT app_security.has_permission('integrations.ai_call')
       OR NOT app_security.can_read_complaint_id(p_complaint,p_created) THEN RETURN false; END IF;
    UPDATE public.ai_insights SET review_status=CASE WHEN p_accept THEN 'accepted' ELSE 'rejected' END,
       reviewed_by_staff_id=app_security.actor_id()
       WHERE id=p_insight AND complaint_id=p_complaint AND complaint_created_at=p_created
         AND review_status='suggested';
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,
        complaint_id,complaint_created_at,after_state)
      VALUES(app_security.actor_id(),CASE WHEN p_accept THEN 'ai_suggestion_accepted'
        ELSE 'ai_suggestion_rejected' END,'ai_insight',p_insight,p_complaint,p_created,
        jsonb_build_object('approved',p_accept));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.create_call_job(
    p_id uuid,p_attempt integer,p_provider_id text,p_outcome text,p_ciphertext bytea,
    p_recording_key text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR length(p_outcome)>80 OR (p_ciphertext IS NOT NULL AND octet_length(p_ciphertext)<28)
       OR length(COALESCE(p_recording_key,''))>512 THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id=p_id AND q.job_kind='voice_followup'
        AND q.state='processing' AND q.attempts=p_attempt FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.call_logs SET provider_call_id=left(p_provider_id,256),
        transcript_ciphertext=p_ciphertext,recording_storage_key=p_recording_key,outcome=p_outcome,
        started_at=COALESCE(started_at,now()),
        ended_at=CASE WHEN p_outcome='initiated' THEN NULL ELSE now() END,
        status=CASE WHEN p_outcome='initiated' THEN 'ringing'
            WHEN p_outcome LIKE 'human_%' OR p_outcome LIKE '%fallback%' THEN 'human_fallback'
            WHEN p_outcome='failed' THEN 'failed' ELSE 'completed' END
      WHERE id=j.call_log_id;
    INSERT INTO public.complaint_events(complaint_id,complaint_created_at,event_type,
        actor_staff_id,metadata)
      VALUES(j.complaint_id,j.complaint_created_at,
        CASE WHEN p_outcome='initiated' THEN 'voice_call_started' ELSE 'voice_call_completed' END,
        j.sender_staff_id,jsonb_build_object('call_log_id',j.call_log_id,'outcome',p_outcome));
    UPDATE public.integration_jobs SET state='sent',provider_message_id=p_provider_id,
        finished_at=now(),locked_until=NULL,last_error_code=NULL WHERE id=p_id;
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.complete_inbound_event(p_id uuid,p_attempt integer,p_data jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE; v_complaint public.complaints%ROWTYPE;
    v_message uuid; v_channel public.communication_channel; v_direction public.message_direction;
    v_transcript bytea;
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

CREATE FUNCTION app_security.list_integration_dead_letters(p_limit integer DEFAULT 50)
RETURNS TABLE (job_id uuid,job_kind text,company_id uuid,complaint_id uuid,
    error_code text,attempts integer,created_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope()<>'staff' OR NOT app_security.has_permission('integrations.manage')
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    RETURN QUERY SELECT d.job_id,d.job_kind,j.company_id,j.complaint_id,d.error_code,
        d.attempts,d.created_at FROM public.integration_dead_letters d
      JOIN public.integration_jobs j ON j.id=d.job_id
      WHERE d.replayed_at IS NULL AND (j.complaint_id IS NULL
        OR app_security.can_read_complaint_id(j.complaint_id,j.complaint_created_at))
        AND (j.company_id IS NULL OR app_security.integration_company_allowed(j.company_id))
      ORDER BY d.created_at DESC LIMIT p_limit;
END;
$function$;

CREATE FUNCTION app_security.requeue_integration_job(p_id uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE j public.integration_jobs%ROWTYPE;
BEGIN
    IF app_security.actor_scope()<>'staff' OR NOT app_security.has_permission('integrations.manage')
       THEN RETURN false; END IF;
    SELECT * INTO j FROM public.integration_jobs q WHERE q.id=p_id AND q.state='dead_letter'
       FOR UPDATE;
    IF NOT FOUND OR (j.company_id IS NOT NULL
       AND NOT app_security.integration_company_allowed(j.company_id))
       OR (j.complaint_id IS NOT NULL
       AND NOT app_security.can_read_complaint_id(j.complaint_id,j.complaint_created_at)) THEN
        RETURN false;
    END IF;
    UPDATE public.integration_jobs SET state='queued',attempts=0,available_at=now(),
       locked_until=NULL,last_error_code=NULL,finished_at=NULL WHERE id=p_id;
    UPDATE public.integration_dead_letters SET replayed_at=now(),
       replayed_by_staff_id=app_security.actor_id() WHERE job_id=p_id AND replayed_at IS NULL;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
       VALUES(app_security.actor_id(),'integration_job_requeued','integration_job',p_id,
          jsonb_build_object('previous_attempts',j.attempts));
    RETURN true;
END;
$function$;

-- Worker-only IMAP polling. The worker gets encrypted credentials and can enqueue
-- a signed-internal event; it cannot directly SELECT provider credentials.
CREATE FUNCTION app_security.claim_imap_configs(p_limit integer DEFAULT 20)
RETURNS TABLE (company_id uuid,config_ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    RETURN QUERY SELECT i.company_id,i.config_ciphertext FROM public.integrations_config i
       WHERE i.provider='imap' AND i.is_enabled AND i.config_ciphertext IS NOT NULL
       ORDER BY i.updated_at DESC LIMIT p_limit;
END;
$function$;

CREATE FUNCTION app_security.enqueue_worker_inbound(
    p_company uuid,p_job_kind text,p_ciphertext bytea,p_idempotency bytea,p_digest bytea)
RETURNS TABLE(job_id uuid,created boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_created boolean;
BEGIN
    IF session_user<>'app_worker' OR app_security.actor_scope()<>'worker'
       OR p_job_kind<>'inbound_email' OR octet_length(p_ciphertext)<28
       OR octet_length(p_idempotency)<>32 OR octet_length(p_digest)<>32 THEN
        RAISE EXCEPTION 'worker inbound queue forbidden' USING ERRCODE='42501';
    END IF;
    INSERT INTO public.integration_jobs(job_kind,provider,company_id,payload_ciphertext,
        idempotency_digest,payload_digest)
      VALUES('inbound_email','imap',p_company,p_ciphertext,p_idempotency,p_digest)
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

CREATE FUNCTION app_security.set_voice_followup_consent(
    p_complaint uuid,p_created timestamptz,p_enabled boolean)
RETURNS timestamptz LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_customer uuid; v_consented_at timestamptz;
BEGIN
    IF app_security.actor_scope()<>'customer'
       OR NOT app_security.can_read_complaint_id(p_complaint,p_created) THEN
        RAISE EXCEPTION 'customer consent update forbidden' USING ERRCODE='42501';
    END IF;
    SELECT c.customer_id INTO v_customer FROM public.complaints c
      WHERE c.id=p_complaint AND c.created_at=p_created;
    IF NOT FOUND OR v_customer<>app_security.actor_id() THEN
        RAISE EXCEPTION 'complaint unavailable' USING ERRCODE='42501';
    END IF;
    UPDATE public.customers SET voice_followup_opt_in=p_enabled,
        voice_consent_at=CASE WHEN p_enabled THEN now() ELSE NULL END,
        updated_at=now() WHERE id=v_customer
        RETURNING voice_consent_at INTO v_consented_at;
    INSERT INTO public.complaint_events(complaint_id,complaint_created_at,event_type,
        actor_customer_id,visibility,metadata)
      VALUES(p_complaint,p_created,'voice_consent_changed',app_security.actor_id(),'internal',
        jsonb_build_object('enabled',p_enabled,'consent_at',v_consented_at));
    INSERT INTO public.audit_logs(action,resource_type,resource_id,complaint_id,
        complaint_created_at,after_state)
      VALUES('customer_voice_consent_updated','customer',v_customer,p_complaint,p_created,
        jsonb_build_object('enabled',p_enabled,'consent_at',v_consented_at));
    RETURN v_consented_at;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.save_integration_config(uuid,text,bytea,boolean,boolean),
    app_security.list_integration_configs(),app_security.webhook_integration_config(uuid,text),
    app_security.enqueue_inbound_job(uuid,text,text,bytea,bytea,bytea),
    app_security.create_integration_job(text,uuid,uuid,timestamptz,uuid,uuid,uuid,bytea,bytea,bytea,timestamptz),
    app_security.integration_job_status(uuid),app_security.save_webhook(uuid,uuid,text,text[],bytea,boolean),
    app_security.list_webhooks(),app_security.webhooks_for_event(uuid,timestamptz,text),
    app_security.enqueue_webhook_job(uuid,uuid,timestamptz,uuid,text,bytea,bytea,bytea),
    app_security.create_api_key(text,bytea,text[],boolean,uuid[],timestamptz),
    app_security.list_api_keys(),app_security.revoke_api_key(uuid),
    app_security.api_key_identity(text,bytea),app_security.api_key_ticket(text,bytea,text),
    app_security.create_call_log(uuid,uuid,timestamptz,uuid,timestamptz),
    app_security.company_contact_emails(uuid),app_security.review_ai_insight(uuid,timestamptz,uuid,boolean),
    app_security.list_integration_dead_letters(integer),app_security.requeue_integration_job(uuid),
    app_security.set_voice_followup_consent(uuid,timestamptz,boolean)
    TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.claim_integration_jobs(integer),
    app_security.webhook_delivery_secrets(uuid),
    app_security.clean_job_attachments(uuid,integer,uuid[]),
    app_security.whatsapp_session_open(uuid,integer),
    app_security.finish_integration_job(uuid,integer,text,text,text),
    app_security.complete_ai_job(uuid,integer,jsonb),
    app_security.create_call_job(uuid,integer,text,text,bytea,text),
    app_security.complete_inbound_event(uuid,integer,jsonb),
    app_security.claim_imap_configs(integer),
    app_security.enqueue_worker_inbound(uuid,text,bytea,bytea,bytea)
    TO app_worker;
