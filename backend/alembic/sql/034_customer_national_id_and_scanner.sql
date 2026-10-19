-- Customer-profile national ID reveal, scanner presence probe, worker logo access.

-- The worker embeds the brand logo in queued outbound email; grant it the
-- same public logo read the runtime already has.
GRANT EXECUTE ON FUNCTION app_security.public_platform_logo() TO app_worker;

-- Directory rows carry only the last four digits; the full national ID is
-- never part of the directory payload.
CREATE OR REPLACE FUNCTION app_security.customer_directory()
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.is_active_staff() AND app_security.has_permission('users.view')
        AND app_security.has_global_scope() THEN COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'full_name', c.full_name, 'email', c.email, 'mobile', c.mobile,
            'whatsapp_available', c.whatsapp_available, 'created_at', c.created_at,
            'national_id_last4', c.national_id_last4,
            'companies', COALESCE((SELECT jsonb_agg(DISTINCT jsonb_build_object(
                'id', co.id, 'name_en', co.name_en, 'name_ar', co.name_ar))
                FROM public.insurance_companies co
                WHERE co.id IN (
                    SELECT p.company_id FROM public.policies p WHERE p.customer_id = c.id
                    UNION
                    SELECT x.company_id FROM public.complaints x WHERE x.customer_id = c.id
                )), '[]'::jsonb),
            'policies', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'company_name_en', co.name_en, 'company_name_ar', co.name_ar,
                'insurance_type', p.insurance_type::text, 'policy_number', p.policy_number,
                'effective_from', p.effective_from, 'expires_at', p.expires_at))
                FROM public.policies p JOIN public.insurance_companies co ON co.id = p.company_id
                WHERE p.customer_id = c.id), '[]'::jsonb),
            'complaints_count', (SELECT count(*) FROM public.complaints x WHERE x.customer_id = c.id),
            'complaints', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'ticket_no', x.ticket_no, 'subject', x.subject, 'status', x.status::text,
                'created_at', x.created_at, 'company_name_en', co.name_en,
                'company_name_ar', co.name_ar, 'insurance_type', x.insurance_type::text)
                ORDER BY x.created_at DESC)
                FROM public.complaints x JOIN public.insurance_companies co ON co.id = x.company_id
                WHERE x.customer_id = c.id), '[]'::jsonb)
        ) ORDER BY c.created_at DESC) FROM public.customers c), '[]'::jsonb) ELSE '[]'::jsonb END
$function$;

-- Full-profile reveal: ciphertext leaves the database only for an active
-- staff member who holds BOTH the directory permission with a global scope
-- AND the dedicated full-national-ID permission. Every successful read is
-- audited by the calling service.
CREATE FUNCTION app_security.read_customer_profile_national_id_ciphertext(p_customer_id uuid)
RETURNS bytea
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE result bytea;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('users.view')
       OR NOT app_security.has_permission('complaints.view_full_national_id')
       OR NOT app_security.has_global_scope() THEN
        RETURN NULL;
    END IF;
    SELECT national_id_ciphertext INTO result
    FROM public.customers WHERE id = p_customer_id;
    RETURN result;
END;
$function$;
REVOKE ALL ON FUNCTION app_security.read_customer_profile_national_id_ciphertext(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.read_customer_profile_national_id_ciphertext(uuid)
    TO app_runtime;

-- Presence probe (never returns configuration): uploads may be marked clean
-- by content inspection alone only when no virus scanner is configured.
CREATE FUNCTION app_security.virus_scanner_configured() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT EXISTS (
        SELECT 1 FROM public.integrations_config i
        WHERE i.provider = 'clamav' AND i.is_enabled AND i.config_ciphertext IS NOT NULL
    )
$function$;
REVOKE ALL ON FUNCTION app_security.virus_scanner_configured() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.virus_scanner_configured() TO app_runtime, app_worker;
