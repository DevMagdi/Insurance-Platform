-- Narrow actor-bound write capability; no general policy INSERT grant.
CREATE FUNCTION app_security.ensure_customer_policy(p_company uuid, p_type public.insurance_type,
    p_number text) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() IS DISTINCT FROM 'customer'
       OR app_security.actor_id() IS NULL THEN
        RAISE EXCEPTION 'Customer sign-in required' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.insurance_companies
        WHERE id = p_company AND is_active AND p_type = ANY(supported_types)) THEN
        RAISE EXCEPTION 'Company unavailable' USING ERRCODE = '23514';
    END IF;
    INSERT INTO public.policies(customer_id, company_id, insurance_type, policy_number)
    VALUES (app_security.actor_id(), p_company, p_type, p_number)
    ON CONFLICT (company_id, policy_number) DO NOTHING;
    SELECT id INTO v_id FROM public.policies
    WHERE customer_id = app_security.actor_id() AND company_id = p_company
      AND insurance_type = p_type AND policy_number = p_number;
    RETURN v_id;
END;
$function$;
REVOKE ALL ON FUNCTION app_security.ensure_customer_policy(uuid, public.insurance_type, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.ensure_customer_policy(uuid, public.insurance_type, text) TO app_runtime;

CREATE OR REPLACE FUNCTION app_security.customer_directory()
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.is_active_staff() AND app_security.has_permission('users.view')
        AND app_security.has_global_scope() THEN COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'full_name', c.full_name, 'email', c.email, 'mobile', c.mobile,
            'whatsapp_available', c.whatsapp_available, 'created_at', c.created_at,
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

