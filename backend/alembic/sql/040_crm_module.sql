-- Revision 040: isolated CRM module tables (crm_*).
--
-- Deliberately separate from the brokerage business tables (complaints,
-- policies, the B2B sales pipeline in `leads`): this module tracks imported
-- person-level leads end to end — Excel import batches with per-row errors,
-- the leads themselves, and their activity timeline. Expand-only like every
-- revision here; the Python downgrade removes only these crm_* tables.
--
-- De-duplication is a database invariant, not an import-time hope: a partial
-- unique index per tenant on phone and on lower(email). The import service
-- still pre-checks so a duplicate becomes a per-row error, never a 500.

CREATE TABLE public.crm_import_batches (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    file_name text NOT NULL,
    uploaded_by uuid REFERENCES public.staff_users(id),
    total_rows integer NOT NULL DEFAULT 0 CHECK (total_rows >= 0),
    success_rows integer NOT NULL DEFAULT 0 CHECK (success_rows >= 0),
    failed_rows integer NOT NULL DEFAULT 0 CHECK (failed_rows >= 0),
    status text NOT NULL DEFAULT 'completed'
        CHECK (status IN ('processing', 'completed', 'completed_with_errors', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.crm_leads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-0000000000a1'::uuid
        REFERENCES public.tenants(id),
    full_name text NOT NULL,
    phone text,
    email text,
    source text NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'whatsapp', 'call', 'email', 'portal', 'import', 'other')),
    status text NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'contacted', 'qualified', 'converted', 'lost')),
    assigned_to uuid REFERENCES public.staff_users(id),
    notes text,
    import_batch_id uuid REFERENCES public.crm_import_batches(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX uq_crm_leads_tenant_phone
    ON public.crm_leads (tenant_id, phone) WHERE phone IS NOT NULL;
CREATE UNIQUE INDEX uq_crm_leads_tenant_email
    ON public.crm_leads (tenant_id, lower(email)) WHERE email IS NOT NULL;
CREATE INDEX ix_crm_leads_status ON public.crm_leads (tenant_id, status);
CREATE INDEX ix_crm_leads_source ON public.crm_leads (tenant_id, source);
CREATE INDEX ix_crm_leads_assigned ON public.crm_leads (assigned_to);
CREATE INDEX ix_crm_leads_created ON public.crm_leads (created_at DESC);
CREATE INDEX ix_crm_leads_name_search ON public.crm_leads (lower(full_name));

CREATE TABLE public.crm_import_errors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id uuid NOT NULL REFERENCES public.crm_import_batches(id) ON DELETE CASCADE,
    row_number integer NOT NULL CHECK (row_number >= 1),
    reason text NOT NULL,
    raw_data jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_crm_import_errors_batch ON public.crm_import_errors (batch_id, row_number);

CREATE TABLE public.crm_activities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id uuid NOT NULL REFERENCES public.crm_leads(id) ON DELETE CASCADE,
    type text NOT NULL
        CHECK (type IN ('call', 'note', 'status_change', 'whatsapp', 'email', 'meeting', 'assignment')),
    description text NOT NULL DEFAULT '',
    created_by uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_crm_activities_lead ON public.crm_activities (lead_id, created_at DESC);
CREATE INDEX ix_crm_activities_created ON public.crm_activities (created_at DESC);

-- Row-level security: staff actor scope only, like the other sales tables.
-- Permission gates (crm.view / crm.manage) are enforced in the API.
ALTER TABLE public.crm_import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crm_import_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY crm_import_batches_staff ON public.crm_import_batches TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.crm_leads ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crm_leads FORCE ROW LEVEL SECURITY;
CREATE POLICY crm_leads_staff ON public.crm_leads TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.crm_import_errors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crm_import_errors FORCE ROW LEVEL SECURITY;
CREATE POLICY crm_import_errors_staff ON public.crm_import_errors TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

ALTER TABLE public.crm_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.crm_activities FORCE ROW LEVEL SECURITY;
CREATE POLICY crm_activities_staff ON public.crm_activities TO app_runtime
    USING (app_security.actor_scope() = 'staff')
    WITH CHECK (app_security.actor_scope() = 'staff');

-- DELETE is granted for this module only: removing a CRM lead (with its
-- activities) is an explicit product operation and is audit-logged in the API.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON public.crm_leads, public.crm_import_batches, public.crm_import_errors, public.crm_activities
    TO app_runtime;
