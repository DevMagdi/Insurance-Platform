-- Revision 039: Phase 1 completions + Phase 2 (Sales CRM, issuing, policy
-- lifecycle, commissions, payments, Excel imports) on a multi-tenant base.
--
-- Tenancy: `tenants` is the brokerage-level tenant. The deployment that
-- already runs this platform becomes the default tenant; every new table
-- carries tenant_id (defaulted, expand-only) and corporate_clients gains one
-- too. Existing hot tables (complaints/customers) keep their established
-- staff-scope RLS and belong to the default tenant by deployment.
--
-- Status-like fields are checked TEXT, not native enums: pipeline stages are
-- tenant-customisable data, not schema. Expand-only throughout.

CREATE TABLE public.tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.tenants (id, slug, name)
VALUES ('00000000-0000-4000-8000-0000000000a1', 'default', 'Default brokerage')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE public.corporate_clients
    ADD COLUMN tenant_id uuid DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid;

ALTER TABLE public.corporate_clients
    ADD CONSTRAINT corporate_clients_tenant_fk
    FOREIGN KEY (tenant_id) REFERENCES public.tenants(id) NOT VALID;

ALTER TABLE public.corporate_users
    ADD COLUMN is_owner boolean NOT NULL DEFAULT false;

CREATE TABLE public.pipeline_stages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    code text NOT NULL,
    name_ar text NOT NULL,
    name_en text NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    is_won boolean NOT NULL DEFAULT false,
    is_lost boolean NOT NULL DEFAULT false,
    is_converted boolean NOT NULL DEFAULT false,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, code),
    CHECK (NOT (is_won AND is_lost) AND NOT (is_won AND is_converted)
           AND NOT (is_lost AND is_converted))
);

INSERT INTO public.pipeline_stages (code, name_ar, name_en, sort_order, is_won, is_lost, is_converted) VALUES
    ('new', 'جديد', 'New', 10, false, false, false),
    ('contacted', 'تم التواصل', 'Contacted', 20, false, false, false),
    ('meeting', 'في ميتنج', 'Meeting', 30, false, false, false),
    ('needs_info', 'يحتاج معلومات / عرض سعر', 'Needs info / quotation', 40, false, false, false),
    ('quote_sent', 'عرض مرسل', 'Quote sent', 50, false, false, false),
    ('negotiation', 'تفاوض', 'Negotiation', 60, false, false, false),
    ('won', 'ربح', 'Won', 70, true, false, false),
    ('lost', 'خسارة', 'Lost', 80, false, true, false),
    ('converted', 'محوّل لعميل', 'Converted to client', 90, false, false, true)
ON CONFLICT (tenant_id, code) DO NOTHING;

CREATE TABLE public.leads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    lead_no text NOT NULL UNIQUE,
    company_name text NOT NULL,
    contact_name text,
    mobile text,
    email text,
    source text NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'whatsapp', 'call', 'email', 'portal')),
    insurance_type text NOT NULL DEFAULT 'general'
        CHECK (insurance_type IN ('health', 'general', 'both')),
    insurer_company_id uuid REFERENCES public.insurance_companies(id),
    stage_code text NOT NULL DEFAULT 'new',
    assigned_staff_id uuid REFERENCES public.staff_users(id),
    expected_premium numeric(14, 2) CHECK (expected_premium IS NULL OR expected_premium >= 0),
    notes text,
    lost_reason text,
    converted_corporate_client_id uuid REFERENCES public.corporate_clients(id),
    converted_at timestamptz,
    next_followup_at timestamptz,
    created_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_leads_tenant_stage ON public.leads (tenant_id, stage_code);
CREATE INDEX ix_leads_assigned_followup ON public.leads (assigned_staff_id, next_followup_at);

CREATE TABLE public.lead_activities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    lead_id uuid NOT NULL REFERENCES public.leads(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('call', 'meeting', 'note', 'task', 'appointment')),
    title text NOT NULL,
    body text,
    due_at timestamptz,
    done_at timestamptz,
    outcome text,
    created_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_lead_activities_lead ON public.lead_activities (lead_id, created_at DESC);

CREATE TABLE public.issuing_checklist_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    insurance_type text NOT NULL CHECK (insurance_type IN ('health', 'general', 'both')),
    document_name text NOT NULL,
    is_required boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, insurance_type, document_name)
);

INSERT INTO public.issuing_checklist_templates (insurance_type, document_name, sort_order) VALUES
    ('health', 'قائمة الموظفين المؤمّن عليهم', 10),
    ('health', 'صور بطاقات الرقم القومي للموظفين', 20),
    ('health', 'السجل التجاري', 30),
    ('health', 'البطاقة الضريبية', 40),
    ('health', 'نموذج طلب التأمين الطبي موقّع', 50),
    ('general', 'السجل التجاري', 10),
    ('general', 'البطاقة الضريبية', 20),
    ('general', 'وصف الخطر / تقرير المعاينة', 30),
    ('general', 'نموذج طلب التأمين العام موقّع', 40),
    ('both', 'السجل التجاري', 10),
    ('both', 'البطاقة الضريبية', 20),
    ('both', 'قائمة الموظفين المؤمّن عليهم', 30),
    ('both', 'وصف الخطر / تقرير المعاينة', 40)
ON CONFLICT (tenant_id, insurance_type, document_name) DO NOTHING;

CREATE TABLE public.issuing_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    lead_id uuid REFERENCES public.leads(id),
    corporate_client_id uuid REFERENCES public.corporate_clients(id),
    insurer_company_id uuid NOT NULL REFERENCES public.insurance_companies(id),
    insurance_type text NOT NULL CHECK (insurance_type IN ('health', 'general', 'both')),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'internal_review', 'sent_to_insurer', 'issued', 'rejected')),
    assigned_staff_id uuid REFERENCES public.staff_users(id),
    insurer_reference text,
    submitted_at timestamptz,
    issued_at timestamptz,
    notes text,
    created_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.issuing_checklist_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    issuing_request_id uuid NOT NULL REFERENCES public.issuing_requests(id) ON DELETE CASCADE,
    document_name text NOT NULL,
    is_required boolean NOT NULL DEFAULT true,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'received', 'verified', 'rejected')),
    received_at timestamptz,
    verified_by uuid REFERENCES public.staff_users(id),
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.brokerage_policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    corporate_client_id uuid NOT NULL REFERENCES public.corporate_clients(id),
    insurer_company_id uuid NOT NULL REFERENCES public.insurance_companies(id),
    policy_no text NOT NULL,
    insurance_type text NOT NULL CHECK (insurance_type IN ('health', 'general', 'both')),
    starts_on date NOT NULL,
    ends_on date NOT NULL CHECK (ends_on >= starts_on),
    premium numeric(14, 2) NOT NULL CHECK (premium > 0),
    currency text NOT NULL DEFAULT 'EGP',
    installment_plan text NOT NULL DEFAULT 'cash'
        CHECK (installment_plan IN ('monthly', 'quarterly', 'semi_annual', 'cash')),
    commission_rate numeric(6, 4) NOT NULL DEFAULT 0.05
        CHECK (commission_rate >= 0 AND commission_rate <= 1),
    commission_amount numeric(14, 2) NOT NULL CHECK (commission_amount >= 0),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'cancelled', 'renewed')),
    renewal_alert_sent_at timestamptz,
    issuing_request_id uuid REFERENCES public.issuing_requests(id),
    created_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, policy_no)
);

CREATE INDEX ix_brokerage_policies_renewal ON public.brokerage_policies (status, ends_on);

CREATE TABLE public.policy_installments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    policy_id uuid NOT NULL REFERENCES public.brokerage_policies(id) ON DELETE CASCADE,
    sequence_no integer NOT NULL CHECK (sequence_no >= 1),
    due_date date NOT NULL,
    amount numeric(14, 2) NOT NULL CHECK (amount >= 0),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'overdue', 'waived')),
    paid_at timestamptz,
    payment_provider text CHECK (payment_provider IS NULL
        OR payment_provider IN ('fawry', 'paymob', 'manual')),
    payment_reference text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (policy_id, sequence_no)
);

CREATE TABLE public.commission_settlements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    policy_id uuid NOT NULL REFERENCES public.brokerage_policies(id) ON DELETE CASCADE,
    insurer_company_id uuid NOT NULL REFERENCES public.insurance_companies(id),
    expected_amount numeric(14, 2) NOT NULL CHECK (expected_amount >= 0),
    received_amount numeric(14, 2) NOT NULL DEFAULT 0 CHECK (received_amount >= 0),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'partial', 'settled')),
    settled_at timestamptz,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.payment_transactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    installment_id uuid NOT NULL REFERENCES public.policy_installments(id),
    provider text NOT NULL CHECK (provider IN ('fawry', 'paymob', 'manual')),
    provider_reference text NOT NULL UNIQUE,
    amount numeric(14, 2) NOT NULL CHECK (amount >= 0),
    status text NOT NULL DEFAULT 'initiated'
        CHECK (status IN ('initiated', 'paid', 'failed')),
    raw_status text,
    paid_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.import_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    kind text NOT NULL CHECK (kind IN ('employees', 'clients', 'leads')),
    filename text NOT NULL,
    corporate_client_id uuid REFERENCES public.corporate_clients(id),
    rows_total integer NOT NULL DEFAULT 0,
    rows_succeeded integer NOT NULL DEFAULT 0,
    rows_failed integer NOT NULL DEFAULT 0,
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Row-level security: staff actor scope only (permission gates + insurer
-- scope are repeated in the API repository). The worker scope may touch the
-- lifecycle columns its Celery jobs own.
ALTER TABLE public.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_staff ON public.tenants TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.pipeline_stages ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pipeline_stages FORCE ROW LEVEL SECURITY;
CREATE POLICY pipeline_stages_staff ON public.pipeline_stages TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.leads FORCE ROW LEVEL SECURITY;
CREATE POLICY leads_staff ON public.leads TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');
CREATE POLICY leads_worker ON public.leads TO app_worker
    USING (app_security.actor_scope() = 'worker');

ALTER TABLE public.lead_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lead_activities FORCE ROW LEVEL SECURITY;
CREATE POLICY lead_activities_staff ON public.lead_activities TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.issuing_checklist_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.issuing_checklist_templates FORCE ROW LEVEL SECURITY;
CREATE POLICY issuing_templates_staff ON public.issuing_checklist_templates TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.issuing_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.issuing_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY issuing_requests_staff ON public.issuing_requests TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.issuing_checklist_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.issuing_checklist_items FORCE ROW LEVEL SECURITY;
CREATE POLICY issuing_items_staff ON public.issuing_checklist_items TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.brokerage_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.brokerage_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY brokerage_policies_staff ON public.brokerage_policies TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');
CREATE POLICY brokerage_policies_worker ON public.brokerage_policies TO app_worker
    USING (app_security.actor_scope() = 'worker')
    WITH CHECK (app_security.actor_scope() = 'worker');

ALTER TABLE public.policy_installments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.policy_installments FORCE ROW LEVEL SECURITY;
CREATE POLICY policy_installments_staff ON public.policy_installments TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');
CREATE POLICY policy_installments_worker ON public.policy_installments TO app_worker
    USING (app_security.actor_scope() = 'worker')
    WITH CHECK (app_security.actor_scope() = 'worker');

ALTER TABLE public.commission_settlements ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.commission_settlements FORCE ROW LEVEL SECURITY;
CREATE POLICY commission_settlements_staff ON public.commission_settlements TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.payment_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_transactions FORCE ROW LEVEL SECURITY;
CREATE POLICY payment_transactions_staff ON public.payment_transactions TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.import_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.import_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY import_jobs_staff ON public.import_jobs TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

-- One-click Lead -> client conversion, carrying every lead field across.
CREATE FUNCTION app_security.convert_lead_to_client(
    p_lead_id uuid, p_slug text, p_name_ar text, p_name_en text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_lead public.leads%ROWTYPE;
    v_client uuid;
BEGIN
    IF NOT app_security.has_permission('crm.convert') THEN
        RAISE EXCEPTION 'lead conversion forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_lead FROM public.leads WHERE id = p_lead_id FOR UPDATE;
    IF NOT FOUND OR v_lead.converted_corporate_client_id IS NOT NULL THEN
        RAISE EXCEPTION 'lead not convertible' USING ERRCODE = 'P0002';
    END IF;
    INSERT INTO public.corporate_clients(
        slug, name_ar, name_en, contact_name, contact_email, contact_mobile, tenant_id)
    VALUES (lower(btrim(p_slug)), btrim(p_name_ar), btrim(p_name_en),
        v_lead.contact_name, v_lead.email, v_lead.mobile, v_lead.tenant_id)
    RETURNING id INTO v_client;
    UPDATE public.leads
    SET converted_corporate_client_id = v_client, converted_at = now(),
        stage_code = 'converted', updated_at = now()
    WHERE id = p_lead_id;
    RETURN v_client;
END;
$function$;

-- Excel import sinks. Importing an employee links (or creates) the customer
-- row the service desk already keys requests on; national IDs arrive only as
-- ciphertext + blind index computed by the API protector.
CREATE FUNCTION app_security.import_insured_employee(
    p_full_name text, p_mobile text, p_email text, p_employee_code text,
    p_corporate_client_id uuid, p_national_id_ciphertext bytea,
    p_national_id_blind bytea, p_national_id_last4 text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('imports.manage') THEN
        RAISE EXCEPTION 'imports forbidden' USING ERRCODE = '42501';
    END IF;
    IF p_national_id_blind IS NOT NULL THEN
        SELECT id INTO v_id FROM public.customers
        WHERE national_id_blind_index = p_national_id_blind;
    END IF;
    IF v_id IS NOT NULL THEN
        UPDATE public.customers
        SET full_name = btrim(p_full_name), mobile = p_mobile,
            email = COALESCE(p_email, email), corporate_client_id = p_corporate_client_id,
            employee_code = COALESCE(p_employee_code, employee_code), updated_at = now()
        WHERE id = v_id;
        RETURN v_id;
    END IF;
    INSERT INTO public.customers(
        full_name, mobile, email, employee_code, corporate_client_id,
        national_id_ciphertext, national_id_blind_index, national_id_last4)
    VALUES (btrim(p_full_name), p_mobile, p_email, p_employee_code, p_corporate_client_id,
        p_national_id_ciphertext, p_national_id_blind, p_national_id_last4)
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.import_corporate_client(
    p_slug text, p_name text, p_contact_name text, p_contact_email text,
    p_contact_mobile text, p_tenant_id uuid
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('imports.manage') THEN
        RAISE EXCEPTION 'imports forbidden' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.corporate_clients(
        slug, name_ar, name_en, contact_name, contact_email, contact_mobile, tenant_id)
    VALUES (lower(btrim(p_slug)), btrim(p_name), btrim(p_name),
        p_contact_name, p_contact_email, p_contact_mobile, p_tenant_id)
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

-- Caller identification for the hotline: employee + company + latest policy.
CREATE FUNCTION app_security.caller_by_mobile(p_mobile text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v jsonb;
BEGIN
    IF NOT (app_security.has_permission('complaints.view')
            OR app_security.has_permission('crm.view')) THEN
        RAISE EXCEPTION 'caller lookup forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT jsonb_build_object(
        'customer_id', c.id, 'full_name', c.full_name, 'mobile', c.mobile,
        'corporate_client_id', c.corporate_client_id,
        'corporate_name_ar', cc.name_ar, 'employee_code', c.employee_code,
        'policy_number', pol.policy_number, 'card_number', pol.card_number,
        'insurer_company_id', pol.company_id)
    INTO v
    FROM public.customers c
    LEFT JOIN public.corporate_clients cc ON cc.id = c.corporate_client_id
    LEFT JOIN LATERAL (
        SELECT * FROM public.policies WHERE customer_id = c.id
        ORDER BY created_at DESC LIMIT 1
    ) pol ON true
    WHERE c.mobile = p_mobile
    ORDER BY c.created_at DESC LIMIT 1;
    RETURN v;
END;
$function$;

-- Signed gateway callbacks land here under the webhook purpose scope; the
-- signature was verified in the API first. Idempotent by provider reference.
CREATE FUNCTION app_security.record_gateway_payment(
    p_reference text, p_provider text, p_paid boolean
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_tx public.payment_transactions%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'webhook' THEN
        RAISE EXCEPTION 'gateway payment forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_tx FROM public.payment_transactions
    WHERE provider_reference = p_reference AND provider = p_provider FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'payment reference not found' USING ERRCODE = 'P0002';
    END IF;
    IF v_tx.status = 'paid' THEN
        RETURN;
    END IF;
    UPDATE public.payment_transactions
    SET status = CASE WHEN p_paid THEN 'paid' ELSE 'failed' END,
        paid_at = CASE WHEN p_paid THEN now() ELSE paid_at END, updated_at = now()
    WHERE id = v_tx.id;
    IF p_paid THEN
        UPDATE public.policy_installments
        SET status = 'paid', paid_at = now(), payment_provider = p_provider,
            payment_reference = p_reference, updated_at = now()
        WHERE id = v_tx.installment_id;
    END IF;
END;
$function$;

-- Permission catalog additions (Phase 2) + spec RBAC roles granted below.
INSERT INTO public.permissions (code, resource, action) VALUES
    ('crm.view', 'crm', 'view'),
    ('crm.manage', 'crm', 'manage'),
    ('crm.convert', 'crm', 'convert'),
    ('policies.view', 'policies', 'view'),
    ('policies.manage', 'policies', 'manage'),
    ('issuing.view', 'issuing', 'view'),
    ('issuing.manage', 'issuing', 'manage'),
    ('imports.manage', 'imports', 'manage'),
    ('payments.manage', 'payments', 'manage')
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.role_permissions (role_id, permission_code)
SELECT r.id, p.code
FROM public.roles r
CROSS JOIN (VALUES ('crm.view'), ('crm.manage'), ('crm.convert'), ('policies.view'),
    ('policies.manage'), ('issuing.view'), ('issuing.manage'), ('imports.manage'),
    ('payments.manage')) AS p(code)
WHERE r.is_system AND r.name = 'administrator'
ON CONFLICT DO NOTHING;

GRANT SELECT, INSERT, UPDATE ON public.tenants, public.pipeline_stages, public.leads,
    public.lead_activities, public.issuing_checklist_templates, public.issuing_requests,
    public.issuing_checklist_items, public.brokerage_policies, public.policy_installments,
    public.commission_settlements, public.payment_transactions, public.import_jobs
    TO app_runtime;
GRANT SELECT ON public.leads TO app_worker;
GRANT SELECT, UPDATE ON public.brokerage_policies, public.policy_installments TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.convert_lead_to_client(uuid, text, text, text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.import_insured_employee(text, text, text, text, uuid, bytea, bytea, text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.import_corporate_client(text, text, text, text, text, uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.caller_by_mobile(text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.record_gateway_payment(text, text, boolean) TO app_runtime;
