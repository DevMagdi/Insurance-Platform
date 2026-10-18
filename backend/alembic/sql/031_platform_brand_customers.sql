-- Persisted white-label identity and narrowly scoped customer directory.
CREATE TABLE platform_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    platform_name text NOT NULL DEFAULT 'Insurance' CHECK (length(btrim(platform_name)) BETWEEN 2 AND 80),
    logo_url text NOT NULL DEFAULT '/insurance-logo.svg' CHECK (length(logo_url) BETWEEN 1 AND 2048),
    logo_mime_type text CHECK (logo_mime_type IN ('image/png', 'image/jpeg', 'image/webp')),
    logo_content bytea CHECK (logo_content IS NULL OR octet_length(logo_content) BETWEEN 1 AND 2097152),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((logo_content IS NULL) = (logo_mime_type IS NULL))
);
INSERT INTO platform_settings(singleton) VALUES (true);
REVOKE ALL ON platform_settings FROM PUBLIC, app_runtime;

-- A single bootstrap identity owns platform-wide branding; a permission grant
-- or administrator role alone never confers this privilege.
CREATE TABLE platform_super_admin (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    staff_user_id uuid NOT NULL UNIQUE REFERENCES staff_users(id) ON DELETE RESTRICT
);
INSERT INTO platform_super_admin(singleton, staff_user_id)
SELECT true, u.id
FROM staff_users u
JOIN user_roles ur ON ur.user_id = u.id
JOIN roles r ON r.id = ur.role_id AND r.name = 'administrator'
JOIN user_company_scope cs ON cs.user_id = u.id AND cs.all_companies
JOIN user_ownership_level ol ON ol.user_id = u.id AND ol.level = 'all'
WHERE NOT EXISTS (SELECT 1 FROM user_insurance_type_scope it WHERE it.user_id = u.id)
ORDER BY u.created_at, u.id
LIMIT 1;
REVOKE ALL ON platform_super_admin FROM PUBLIC, app_runtime;

CREATE FUNCTION app_security.customer_email_matches(p_mobile text, p_email text, p_blind bytea)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.actor_scope() = 'customer_auth' AND EXISTS (
        SELECT 1 FROM public.customers c WHERE c.mobile = p_mobile
          AND lower(c.email) = lower(p_email)
          AND (p_blind IS NULL OR c.national_id_blind_index = p_blind))
$function$;

CREATE FUNCTION app_security.public_platform_brand()
RETURNS TABLE(platform_name text, logo_url text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT s.platform_name,
        CASE WHEN s.logo_content IS NOT NULL THEN '/api/public/brand/logo' ELSE s.logo_url END
    FROM public.platform_settings s WHERE s.singleton
$function$;

CREATE FUNCTION app_security.update_platform_brand(p_name text, p_logo text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_before public.platform_settings%ROWTYPE;
    v_keep_uploaded_logo boolean;
BEGIN
    IF NOT app_security.is_active_staff() OR NOT app_security.has_permission('settings.manage')
       OR NOT app_security.has_global_scope()
       OR NOT EXISTS (SELECT 1 FROM public.platform_super_admin s
            WHERE s.singleton AND s.staff_user_id = app_security.actor_id())
       OR p_name IS NULL OR length(btrim(p_name)) NOT BETWEEN 2 AND 80
       OR p_logo IS NULL OR length(p_logo) NOT BETWEEN 1 AND 2048
       OR p_logo !~ '^/[A-Za-z0-9._/-]+$' OR p_logo LIKE '//%'
       OR p_logo ~ '(^|/)[.][.](/|$)' THEN
        RETURN false;
    END IF;
    SELECT * INTO v_before FROM public.platform_settings WHERE singleton FOR UPDATE;
    v_keep_uploaded_logo := p_logo = '/api/public/brand/logo' AND v_before.logo_content IS NOT NULL;
    IF p_logo = '/api/public/brand/logo' AND NOT v_keep_uploaded_logo THEN
        RETURN false;
    END IF;
    UPDATE public.platform_settings SET platform_name = btrim(p_name),
        logo_url = CASE WHEN v_keep_uploaded_logo THEN logo_url ELSE p_logo END,
        logo_mime_type = CASE WHEN v_keep_uploaded_logo THEN logo_mime_type ELSE NULL END,
        logo_content = CASE WHEN v_keep_uploaded_logo THEN logo_content ELSE NULL END,
        updated_at = now() WHERE singleton;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, before_state, after_state)
        VALUES (app_security.actor_id(), 'platform_brand_updated', 'platform_settings',
        jsonb_build_object('platform_name', v_before.platform_name,
            'logo_url', CASE WHEN v_before.logo_content IS NOT NULL
                THEN '/api/public/brand/logo' ELSE v_before.logo_url END),
        jsonb_build_object('platform_name', btrim(p_name), 'logo_url', p_logo));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.update_platform_logo(p_mime text, p_content bytea)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_before public.platform_settings%ROWTYPE;
BEGIN
    IF NOT app_security.is_active_staff() OR NOT app_security.has_permission('settings.manage')
       OR NOT app_security.has_global_scope()
       OR NOT EXISTS (SELECT 1 FROM public.platform_super_admin s
            WHERE s.singleton AND s.staff_user_id = app_security.actor_id())
       OR p_mime NOT IN ('image/png', 'image/jpeg', 'image/webp')
       OR p_content IS NULL OR octet_length(p_content) NOT BETWEEN 1 AND 2097152 THEN
        RETURN false;
    END IF;
    SELECT * INTO v_before FROM public.platform_settings WHERE singleton FOR UPDATE;
    UPDATE public.platform_settings SET logo_mime_type = p_mime, logo_content = p_content,
        updated_at = now() WHERE singleton;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type, before_state, after_state)
        VALUES (app_security.actor_id(), 'platform_logo_uploaded', 'platform_settings',
        jsonb_build_object('platform_name', v_before.platform_name,
            'logo_url', CASE WHEN v_before.logo_content IS NOT NULL
                THEN '/api/public/brand/logo' ELSE v_before.logo_url END),
        jsonb_build_object('platform_name', v_before.platform_name,
            'logo_url', '/api/public/brand/logo', 'logo_mime_type', p_mime,
            'logo_bytes', octet_length(p_content)));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.public_platform_logo()
RETURNS TABLE(logo_mime_type text, logo_content bytea)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT s.logo_mime_type, s.logo_content FROM public.platform_settings s
    WHERE s.singleton AND s.logo_content IS NOT NULL
$function$;

CREATE FUNCTION app_security.customer_directory()
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.is_active_staff() AND app_security.has_permission('users.view')
        AND app_security.has_global_scope() THEN COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'full_name', c.full_name, 'email', c.email, 'mobile', c.mobile,
            'whatsapp_available', c.whatsapp_available, 'created_at', c.created_at,
            'companies', COALESCE((SELECT jsonb_agg(DISTINCT jsonb_build_object(
                'id', co.id, 'name_en', co.name_en, 'name_ar', co.name_ar))
                FROM public.policies p JOIN public.insurance_companies co ON co.id = p.company_id
                WHERE p.customer_id = c.id), '[]'::jsonb),
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

REVOKE ALL ON FUNCTION app_security.customer_email_matches(text, text, bytea),
    app_security.public_platform_brand(), app_security.update_platform_brand(text, text),
    app_security.update_platform_logo(text, bytea), app_security.public_platform_logo(),
    app_security.customer_directory() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.customer_email_matches(text, text, bytea),
    app_security.public_platform_brand(), app_security.update_platform_brand(text, text),
    app_security.update_platform_logo(text, bytea), app_security.public_platform_logo(),
    app_security.customer_directory() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.public_platform_brand() TO app_worker;
