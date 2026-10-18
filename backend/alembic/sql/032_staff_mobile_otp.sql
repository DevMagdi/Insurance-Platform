-- Authenticated-password staff may load only the encrypted phone profile and
-- the global Twilio SMS credentials needed to deliver their login challenge.
CREATE OR REPLACE FUNCTION app_security.integration_credentials(
    p_company uuid, p_provider text, p_allow_global_fallback boolean DEFAULT true)
RETURNS TABLE (config_id uuid, config_company_id uuid, config_ciphertext bytea)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR (
            session_user = 'app_runtime' AND app_security.actor_scope() = 'staff'
            AND app_security.has_permission('integrations.manage')
            AND ((p_company IS NULL AND app_security.has_global_scope())
                 OR app_security.integration_company_allowed(p_company))
        )
        OR (
            session_user = 'app_runtime' AND app_security.actor_scope() = 'staff_auth'
            AND p_provider = 'twilio_sms' AND p_company IS NULL
            AND p_allow_global_fallback = false
        )
    ) THEN
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

CREATE FUNCTION app_security.staff_profile_ciphertext_for_auth(p_user uuid)
RETURNS bytea
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT p.personal_ciphertext
    FROM public.staff_profiles p
    JOIN public.staff_users u ON u.id = p.staff_user_id
    WHERE p.staff_user_id = p_user AND u.status = 'active'
      AND session_user = 'app_runtime' AND app_security.actor_scope() = 'staff_auth'
$function$;

GRANT EXECUTE ON FUNCTION app_security.staff_profile_ciphertext_for_auth(uuid) TO app_runtime;
