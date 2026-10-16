-- Phase 8 (revision 021): PDPL (Egypt Law 151/2020) consent evidence and data-subject
-- requests, data retention, and queue-depth telemetry for backpressure.
-- Zero-downtime rules (see docs/RUNBOOK.md): additive objects only, no table rewrites,
-- no long ACCESS EXCLUSIVE locks on hot tables, every function guarded.

-- Consent evidence: append-only; the latest row per purpose is the current state.
CREATE TABLE public.consent_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES public.customers(id) ON DELETE CASCADE,
    purpose text NOT NULL,
    granted boolean NOT NULL,
    policy_version text NOT NULL,
    source text NOT NULL DEFAULT 'web',
    ip_address inet,
    user_agent varchar(256),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_consent_purpose CHECK (purpose IN (
        'complaint_processing', 'notifications', 'voice_followup', 'ai_processing',
        'marketing')),
    CONSTRAINT ck_consent_source CHECK (source IN ('web', 'staff', 'import')),
    CONSTRAINT ck_consent_policy_version CHECK (policy_version ~ '^[A-Za-z0-9._-]{1,32}$')
);
CREATE INDEX ix_consent_records_latest
    ON public.consent_records (customer_id, purpose, created_at DESC);
ALTER TABLE public.consent_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.consent_records FORCE ROW LEVEL SECURITY;
CREATE POLICY consent_records_read ON public.consent_records FOR SELECT TO app_runtime
    USING (
        (app_security.actor_scope() = 'customer' AND customer_id = app_security.actor_id())
        OR (app_security.is_active_staff()
            AND app_security.has_permission('settings.manage')
            AND app_security.has_global_scope())
    );
GRANT SELECT ON public.consent_records TO app_runtime;
-- No INSERT/UPDATE/DELETE policy or grant: writes go through record_consent().

CREATE TABLE public.privacy_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES public.customers(id) ON DELETE CASCADE,
    kind text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    details text,
    resolution text,
    handled_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    due_at timestamptz NOT NULL DEFAULT now() + interval '30 days',
    completed_at timestamptz,
    CONSTRAINT ck_privacy_kind CHECK (kind IN ('access', 'erasure', 'rectification', 'objection')),
    CONSTRAINT ck_privacy_status CHECK (status IN ('pending', 'in_progress', 'completed', 'rejected')),
    CONSTRAINT ck_privacy_details CHECK (details IS NULL OR char_length(details) <= 2000),
    CONSTRAINT ck_privacy_resolution CHECK (resolution IS NULL OR char_length(resolution) <= 2000),
    CONSTRAINT ck_privacy_completion CHECK (
        (status IN ('completed', 'rejected')) = (completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX ux_privacy_requests_open
    ON public.privacy_requests (customer_id, kind) WHERE status IN ('pending', 'in_progress');
CREATE INDEX ix_privacy_requests_queue ON public.privacy_requests (status, due_at, id);
ALTER TABLE public.privacy_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.privacy_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY privacy_requests_read ON public.privacy_requests FOR SELECT TO app_runtime
    USING (
        (app_security.actor_scope() = 'customer' AND customer_id = app_security.actor_id())
        OR (app_security.is_active_staff()
            AND app_security.has_permission('settings.manage')
            AND app_security.has_global_scope())
    );
GRANT SELECT ON public.privacy_requests TO app_runtime;

CREATE FUNCTION app_security.record_consent(
    p_purpose text, p_granted boolean, p_policy_version text, p_ip inet, p_user_agent text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'customer' OR app_security.actor_id() IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.consent_records
        (customer_id, purpose, granted, policy_version, source, ip_address, user_agent)
    VALUES (app_security.actor_id(), p_purpose, p_granted, p_policy_version, 'web', p_ip,
        left(p_user_agent, 256))
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.create_privacy_request(p_kind text, p_details text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'customer' OR app_security.actor_id() IS NULL THEN
        RETURN NULL;
    END IF;
    INSERT INTO public.privacy_requests (customer_id, kind, details)
    VALUES (app_security.actor_id(), p_kind, NULLIF(btrim(p_details), ''))
    ON CONFLICT (customer_id, kind) WHERE status IN ('pending', 'in_progress') DO NOTHING
    RETURNING id INTO v_id;
    IF v_id IS NULL THEN
        SELECT id INTO v_id FROM public.privacy_requests
        WHERE customer_id = app_security.actor_id() AND kind = p_kind
          AND status IN ('pending', 'in_progress');
    END IF;
    RETURN v_id;
END;
$function$;

-- Erasure keeps the complaint record (insurance-regulatory retention is a lawful
-- basis under Law 151/2020) but removes direct contact data, disables channels and
-- revokes every session. The encrypted national ID stays only for legal retention.
CREATE FUNCTION app_security.process_privacy_request(
    p_id uuid, p_status text, p_resolution text
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_request public.privacy_requests%ROWTYPE;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('settings.manage')
       OR NOT app_security.has_global_scope()
       OR p_status NOT IN ('in_progress', 'completed', 'rejected') THEN
        RETURN false;
    END IF;
    SELECT * INTO v_request FROM public.privacy_requests WHERE id = p_id FOR UPDATE;
    IF NOT FOUND OR v_request.status IN ('completed', 'rejected') THEN
        RETURN false;
    END IF;
    UPDATE public.privacy_requests SET
        status = p_status,
        resolution = NULLIF(btrim(p_resolution), ''),
        handled_by = app_security.actor_id(),
        completed_at = CASE WHEN p_status IN ('completed', 'rejected') THEN now() END
    WHERE id = p_id;
    IF v_request.kind = 'erasure' AND p_status = 'completed' THEN
        UPDATE public.customers SET
            full_name = 'Erased customer',
            email = NULL,
            whatsapp_available = false,
            notification_preferences = '{"email": false, "whatsapp": false, "sms": false}'::jsonb,
            token_version = token_version + 1,
            updated_at = now()
        WHERE id = v_request.customer_id;
        UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'customer' AND subject_id = v_request.customer_id
          AND revoked_at IS NULL;
    END IF;
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type, resource_id, after_state)
    VALUES (app_security.actor_id(), 'privacy_request.' || p_status, 'privacy_request', p_id,
        jsonb_build_object('kind', v_request.kind, 'status', p_status));
    RETURN true;
END;
$function$;

REVOKE ALL ON FUNCTION app_security.record_consent(text, boolean, text, inet, text),
    app_security.create_privacy_request(text, text),
    app_security.process_privacy_request(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.record_consent(text, boolean, text, inet, text),
    app_security.create_privacy_request(text, text),
    app_security.process_privacy_request(uuid, text, text) TO app_runtime;

-- Worker-only telemetry: depth of each durable queue (drives API backpressure).
CREATE FUNCTION app_security.queue_depths()
RETURNS TABLE (queue text, depth bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker' THEN RETURN; END IF;
    RETURN QUERY
        SELECT 'integration'::text, count(*) FROM public.integration_jobs
            WHERE state IN ('queued', 'processing')
        UNION ALL
        SELECT 'notification'::text, count(*) FROM public.notification_outbox
            WHERE state IN ('queued', 'processing')
        UNION ALL
        SELECT 'insurer'::text, count(*) FROM public.insurer_outbox
            WHERE state IN ('queued', 'processing')
        UNION ALL
        SELECT 'export'::text, count(*) FROM public.export_jobs
            WHERE status IN ('queued', 'running');
END;
$function$;

-- Storage-limitation principle: purge operational records that carry personal data
-- once they have no further purpose. Complaint records follow the regulatory policy.
CREATE FUNCTION app_security.apply_retention(
    p_session_days integer DEFAULT 30, p_outbox_days integer DEFAULT 180
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_sessions bigint; v_notices bigint; v_insurer bigint;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_session_days NOT BETWEEN 7 AND 365 OR p_outbox_days NOT BETWEEN 30 AND 3650 THEN
        RETURN NULL;
    END IF;
    -- Whole refresh-token families only (rotated_from_id references stay valid).
    DELETE FROM public.auth_sessions s WHERE s.family_id IN (
        SELECT f.family_id FROM public.auth_sessions f
        GROUP BY f.family_id
        HAVING bool_and(f.revoked_at IS NOT NULL OR f.expires_at < now())
           AND max(COALESCE(f.revoked_at, f.expires_at))
               < now() - make_interval(days => p_session_days)
        LIMIT 5000);
    GET DIAGNOSTICS v_sessions = ROW_COUNT;
    DELETE FROM public.notification_outbox WHERE id IN (
        SELECT id FROM public.notification_outbox
        WHERE state IN ('sent', 'failed', 'suppressed')
          AND created_at < now() - make_interval(days => p_outbox_days)
        LIMIT 10000);
    GET DIAGNOSTICS v_notices = ROW_COUNT;
    DELETE FROM public.insurer_outbox WHERE id IN (
        SELECT id FROM public.insurer_outbox
        WHERE state IN ('sent', 'failed')
          AND created_at < now() - make_interval(days => p_outbox_days)
        LIMIT 10000);
    GET DIAGNOSTICS v_insurer = ROW_COUNT;
    RETURN jsonb_build_object('auth_sessions', v_sessions, 'notification_outbox', v_notices,
        'insurer_outbox', v_insurer);
END;
$function$;

REVOKE ALL ON FUNCTION app_security.queue_depths(),
    app_security.apply_retention(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.queue_depths(),
    app_security.apply_retention(integer, integer) TO app_worker;
