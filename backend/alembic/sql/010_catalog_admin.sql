-- Catalog writes are through guarded functions; the runtime login has only
-- scope-filtered SELECT on metadata. No raw provider secrets are returned.
CREATE FUNCTION app_security.can_manage_catalog(
    p_company uuid, p_type insurance_type DEFAULT NULL)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
    SELECT app_security.is_active_staff()
        AND CASE WHEN p_company IS NULL THEN app_security.has_global_scope()
                 ELSE app_security.has_company_scope(p_company) END
        AND (NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope s
                         WHERE s.user_id = app_security.actor_id())
             OR (p_type IS NOT NULL AND EXISTS
                (SELECT 1 FROM public.user_insurance_type_scope s
                 WHERE s.user_id = app_security.actor_id() AND s.insurance_type = p_type)))
$function$;

CREATE FUNCTION app_security.save_company(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_before jsonb; v_emails text[]; v_types insurance_type[];
BEGIN
    IF NOT app_security.has_permission('companies.manage')
       OR NOT app_security.has_global_scope() OR jsonb_typeof(p_data) <> 'object'
       OR p_data - ARRAY['slug','name_en','name_ar','contact_emails',
                'supported_types','is_active'] <> '{}'::jsonb
       OR p_data->>'slug' !~ '^[a-z][a-z0-9-]{1,59}$'
       OR length(btrim(p_data->>'name_en')) NOT BETWEEN 2 AND 120
       OR length(btrim(p_data->>'name_ar')) NOT BETWEEN 2 AND 120
       OR jsonb_typeof(p_data->'contact_emails') <> 'array'
       OR jsonb_array_length(p_data->'contact_emails') > 10
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_data->'contact_emails') AS addr
           WHERE addr !~ '^[^@ ]+@[^@ ]+\.[^@ ]+$'
              OR addr <> lower(addr) OR length(addr) > 254)
       OR jsonb_typeof(p_data->'supported_types') <> 'array'
       OR jsonb_array_length(p_data->'supported_types') NOT BETWEEN 1 AND 3
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_data->'supported_types') AS kind
           WHERE kind NOT IN ('health','general','both'))
       OR jsonb_typeof(p_data->'is_active') <> 'boolean' THEN
        RAISE EXCEPTION 'company data forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    SELECT array_agg(addr ORDER BY addr) INTO v_emails
        FROM (SELECT DISTINCT jsonb_array_elements_text(p_data->'contact_emails') addr) x;
    SELECT array_agg(kind::insurance_type ORDER BY kind) INTO v_types
        FROM (SELECT DISTINCT jsonb_array_elements_text(p_data->'supported_types') kind) x;
    IF p_id IS NULL THEN
        INSERT INTO public.insurance_companies
            (slug,name_en,name_ar,contact_emails,supported_types,is_active)
        VALUES (p_data->>'slug', p_data->>'name_en', p_data->>'name_ar',
                COALESCE(v_emails, ARRAY[]::text[]), v_types,
                (p_data->>'is_active')::boolean) RETURNING id INTO v_id;
    ELSE
        SELECT jsonb_build_object('slug', c.slug, 'is_active', c.is_active)
            INTO v_before FROM public.insurance_companies c WHERE c.id = p_id FOR UPDATE;
        IF v_before IS NULL THEN RETURN NULL; END IF;
        UPDATE public.insurance_companies SET slug = p_data->>'slug',
            name_en = p_data->>'name_en', name_ar = p_data->>'name_ar',
            contact_emails = COALESCE(v_emails, ARRAY[]::text[]),
            supported_types = v_types, is_active = (p_data->>'is_active')::boolean,
            updated_at = now() WHERE id = p_id;
        v_id := p_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id, action, resource_type,
        resource_id, before_state, after_state)
        VALUES (app_security.actor_id(), 'company_saved', 'company', v_id,
            COALESCE(v_before, '{}'::jsonb),
            jsonb_build_object('slug', p_data->>'slug',
                'is_active', (p_data->>'is_active')::boolean));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.save_category(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_prior uuid; v_parent uuid;
BEGIN
    v_company := NULLIF(p_data->>'company_id', '')::uuid;
    v_parent := NULLIF(p_data->>'parent_id', '')::uuid;
    IF NOT app_security.has_permission('categories.manage')
       OR NOT app_security.can_manage_catalog(v_company)
       OR length(btrim(p_data->>'name_en')) NOT BETWEEN 2 AND 120
       OR length(btrim(p_data->>'name_ar')) NOT BETWEEN 2 AND 120
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR (v_company IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.insurance_companies WHERE id = v_company AND is_active))
       OR (v_parent IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.categories c WHERE c.id = v_parent AND c.parent_id IS NULL
             AND c.company_id IS NOT DISTINCT FROM v_company))
       OR v_parent = p_id THEN
        RAISE EXCEPTION 'category data forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NOT NULL THEN
        SELECT company_id INTO v_prior FROM public.categories WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_prior IS DISTINCT FROM v_company
           OR NOT app_security.can_manage_catalog(v_prior) THEN
            RAISE EXCEPTION 'category cannot move across scope' USING ERRCODE = '42501';
        END IF;
        UPDATE public.categories SET name_en = p_data->>'name_en',
            name_ar = p_data->>'name_ar', parent_id = v_parent,
            is_active = (p_data->>'is_active')::boolean WHERE id = p_id;
        v_id := p_id;
    ELSE
        INSERT INTO public.categories(company_id,parent_id,name_en,name_ar,is_active)
            VALUES (v_company,v_parent,p_data->>'name_en',p_data->>'name_ar',
                (p_data->>'is_active')::boolean) RETURNING id INTO v_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'category_saved','category',v_id,
            jsonb_build_object('company_id',v_company,'active',p_data->>'is_active'));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.save_sla_rule(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_prior uuid;
    v_type insurance_type; v_old_type insurance_type;
BEGIN
    v_company := NULLIF(p_data->>'company_id', '')::uuid;
    IF p_data->>'insurance_type' NOT IN ('health','general','both')
       AND p_data->>'insurance_type' IS NOT NULL THEN
        RAISE EXCEPTION 'unknown insurance type' USING ERRCODE = '22023';
    END IF;
    v_type := (p_data->>'insurance_type')::insurance_type;
    IF NOT app_security.has_permission('sla.manage')
       OR NOT app_security.can_manage_catalog(v_company, v_type)
       OR ((p_data->>'priority') NOT IN ('low','normal','high','urgent')
           AND p_data->>'priority' IS NOT NULL)
       OR (p_data->>'response_minutes')::integer NOT BETWEEN 1 AND 525600
       OR (p_data->>'resolution_minutes')::integer NOT BETWEEN 1 AND 525600
       OR p_data->'business_hours' <> '{}'::jsonb
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR (v_company IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.insurance_companies WHERE id = v_company AND is_active)) THEN
        RAISE EXCEPTION 'SLA data forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NOT NULL THEN
        SELECT company_id, insurance_type INTO v_prior, v_old_type
            FROM public.sla_rules WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_prior IS DISTINCT FROM v_company OR
           NOT app_security.can_manage_catalog(v_prior, v_old_type) THEN
            RAISE EXCEPTION 'SLA cannot move across scope' USING ERRCODE = '42501';
        END IF;
        UPDATE public.sla_rules SET insurance_type = v_type,
            priority = (p_data->>'priority')::complaint_priority,
            response_minutes = (p_data->>'response_minutes')::integer,
            resolution_minutes = (p_data->>'resolution_minutes')::integer,
            business_hours = '{}'::jsonb,
            is_active = (p_data->>'is_active')::boolean WHERE id = p_id;
        v_id := p_id;
    ELSE
        INSERT INTO public.sla_rules(company_id,insurance_type,priority,
            response_minutes,resolution_minutes,business_hours,is_active)
        VALUES (v_company,v_type,(p_data->>'priority')::complaint_priority,
            (p_data->>'response_minutes')::integer,
            (p_data->>'resolution_minutes')::integer,'{}'::jsonb,
            (p_data->>'is_active')::boolean) RETURNING id INTO v_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'sla_saved','sla_rule',v_id,
            jsonb_build_object('company_id',v_company,'insurance_type',v_type,
                'response_minutes',p_data->>'response_minutes',
                'resolution_minutes',p_data->>'resolution_minutes'));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.save_template(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_company uuid; v_prior uuid;
BEGIN
    v_company := NULLIF(p_data->>'company_id', '')::uuid;
    IF NOT app_security.has_permission('templates.manage')
       OR NOT app_security.can_manage_catalog(v_company)
       OR length(btrim(p_data->>'title')) NOT BETWEEN 2 AND 100
       OR length(btrim(p_data->>'body_en')) NOT BETWEEN 2 AND 2000
       OR length(btrim(p_data->>'body_ar')) NOT BETWEEN 2 AND 2000
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR (v_company IS NOT NULL AND NOT EXISTS
           (SELECT 1 FROM public.insurance_companies WHERE id = v_company AND is_active)) THEN
        RAISE EXCEPTION 'template data forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NOT NULL THEN
        SELECT company_id INTO v_prior FROM public.canned_replies WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_prior IS DISTINCT FROM v_company OR
           NOT app_security.can_manage_catalog(v_prior) THEN
            RAISE EXCEPTION 'template cannot move across scope' USING ERRCODE = '42501';
        END IF;
        UPDATE public.canned_replies SET title = p_data->>'title',
            body_en = p_data->>'body_en', body_ar = p_data->>'body_ar',
            is_active = (p_data->>'is_active')::boolean WHERE id = p_id;
        v_id := p_id;
    ELSE
        INSERT INTO public.canned_replies
            (company_id,title,body_en,body_ar,is_active,created_by_staff_id)
        VALUES (v_company,p_data->>'title',p_data->>'body_en',p_data->>'body_ar',
            (p_data->>'is_active')::boolean,app_security.actor_id()) RETURNING id INTO v_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'template_saved','canned_reply',v_id,
            jsonb_build_object('company_id',v_company,'title',p_data->>'title'));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.admin_companies() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.has_permission('companies.manage')
        AND app_security.has_global_scope() THEN
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
            'id',id,'slug',slug,'name_en',name_en,'name_ar',name_ar,
            'contact_emails',contact_emails,'supported_types',supported_types,
            'is_active',is_active) ORDER BY slug)
          FROM public.insurance_companies), '[]'::jsonb)
        ELSE NULL END
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.save_company(uuid,jsonb),
    app_security.save_category(uuid,jsonb),
    app_security.save_sla_rule(uuid,jsonb),
    app_security.save_template(uuid,jsonb),
    app_security.admin_companies() TO app_runtime;
