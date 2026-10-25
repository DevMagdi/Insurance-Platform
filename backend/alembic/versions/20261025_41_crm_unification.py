"""CRM unification: fold the B2B sales pipeline into the ``crm_*`` module.

Revision ID: 20261025_41
Revises: 20261024_40

This is a deliberate **contract** migration (hence pure Python, not an
expand-only ``.sql`` revision; old binaries must not run against the
migrated schema). It:

- creates ``crm_pipeline_stages`` + ``crm_tasks`` and widens ``crm_leads``
  into the single lead model (pipeline stage, priority, company,
  insurance fields, expected premium, conversion + follow-up columns);
- copies ``pipeline_stages`` / ``leads`` / ``lead_activities`` into the
  ``crm_*`` tables **preserving ids** (``issuing_requests.lead_id`` is
  repointed to ``crm_leads``), with collision-safe phone/email handling
  against the tenant dedup indexes;
- replaces ``app_security.convert_lead_to_client`` to read ``crm_leads``;
- adds ``tenant_id`` to ``crm_activities`` / ``crm_import_errors`` and
  worker-scope access for the Celery CRM reminder job;
- drops ``lead_activities``, ``leads`` and ``pipeline_stages`` and the
  fixed ``crm_leads.status`` lifecycle (mapped onto pipeline stages).

The downgrade recreates the revision-039 tables and copies data back
best-effort (imported person leads come back as company leads named
after the person; sources ``import``/``other`` fold to ``manual``).
"""

from __future__ import annotations

from alembic import op

revision = "20261025_41"
down_revision = "20261024_40"
branch_labels = None
depends_on = None

_TENANT = "'00000000-0000-4000-8000-0000000000a1'::uuid"

_UPGRADE = (
    # --- crm_pipeline_stages (carries pipeline_stages over verbatim) -------
    f"""
    CREATE TABLE public.crm_pipeline_stages (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL DEFAULT {_TENANT}
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
    )
    """,
    """
    INSERT INTO public.crm_pipeline_stages
        (id, tenant_id, code, name_ar, name_en, sort_order, is_won, is_lost,
         is_converted, is_active, created_at, updated_at)
    SELECT id, tenant_id, code, name_ar, name_en, sort_order, is_won, is_lost,
           is_converted, is_active, created_at, updated_at
    FROM public.pipeline_stages
    """,
    """
    INSERT INTO public.crm_pipeline_stages
        (code, name_ar, name_en, sort_order, is_won, is_lost, is_converted) VALUES
        ('new', 'جديد', 'New', 10, false, false, false),
        ('contacted', 'تم التواصل', 'Contacted', 20, false, false, false),
        ('meeting', 'في ميتنج', 'Meeting', 30, false, false, false),
        ('needs_info', 'يحتاج معلومات / عرض سعر', 'Needs info / quotation',
         40, false, false, false),
        ('quote_sent', 'عرض مرسل', 'Quote sent', 50, false, false, false),
        ('negotiation', 'تفاوض', 'Negotiation', 60, false, false, false),
        ('won', 'ربح', 'Won', 70, true, false, false),
        ('lost', 'خسارة', 'Lost', 80, false, true, false),
        ('converted', 'محوّل لعميل', 'Converted to client', 90, false, false, true)
    ON CONFLICT (tenant_id, code) DO NOTHING
    """,
    # --- crm_tasks -----------------------------------------------------------
    f"""
    CREATE TABLE public.crm_tasks (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL DEFAULT {_TENANT}
            REFERENCES public.tenants(id),
        lead_id uuid NOT NULL REFERENCES public.crm_leads(id) ON DELETE CASCADE,
        type text NOT NULL DEFAULT 'task'
            CHECK (type IN ('call', 'meeting', 'email', 'whatsapp', 'task')),
        title text NOT NULL,
        description text,
        due_at timestamptz NOT NULL,
        status text NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'done', 'cancelled')),
        assigned_to uuid REFERENCES public.staff_users(id),
        completed_at timestamptz,
        reminder_sent_at timestamptz,
        created_by uuid REFERENCES public.staff_users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_crm_tasks_lead ON public.crm_tasks (lead_id, status, due_at)",
    "CREATE INDEX ix_crm_tasks_assignee ON public.crm_tasks (assigned_to, status, due_at)",
    "CREATE INDEX ix_crm_tasks_due ON public.crm_tasks (status, due_at)",
    # --- widen crm_leads into the single lead model --------------------------
    """
    ALTER TABLE public.crm_leads
        ADD COLUMN lead_no text,
        ADD COLUMN company_name text,
        ADD COLUMN stage_code text NOT NULL DEFAULT 'new',
        ADD COLUMN priority text NOT NULL DEFAULT 'normal'
            CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
        ADD COLUMN insurance_type text NOT NULL DEFAULT 'general'
            CHECK (insurance_type IN ('health', 'general', 'both')),
        ADD COLUMN insurer_company_id uuid REFERENCES public.insurance_companies(id),
        ADD COLUMN expected_premium numeric(14, 2)
            CHECK (expected_premium IS NULL OR expected_premium >= 0),
        ADD COLUMN lost_reason text,
        ADD COLUMN converted_corporate_client_id uuid
            REFERENCES public.corporate_clients(id),
        ADD COLUMN converted_at timestamptz,
        ADD COLUMN closed_at timestamptz,
        ADD COLUMN next_followup_at timestamptz,
        ADD COLUMN created_by uuid REFERENCES public.staff_users(id)
    """,
    # Existing (import-era) leads: fixed status lifecycle -> pipeline stage.
    """
    UPDATE public.crm_leads SET stage_code = CASE status
        WHEN 'qualified' THEN 'needs_info' ELSE status END,
        closed_at = CASE WHEN status IN ('converted', 'lost') THEN updated_at END
    """,
    """
    UPDATE public.crm_leads
    SET lead_no = 'LEAD-' || to_char(created_at, 'YYYY') || '-'
        || upper(substr(replace(id::text, '-', ''), 1, 6))
    WHERE lead_no IS NULL
    """,
    # Copy the B2B sales leads, preserving ids; phone/email collisions
    # against the tenant dedup indexes (or within the old table, which had
    # no contact uniqueness) are nulled rather than dropping the lead, so
    # issuing_requests keeps its FK target.
    """
    WITH ranked AS (
        SELECT l.*,
            row_number() OVER (PARTITION BY l.tenant_id, l.mobile
                               ORDER BY l.created_at, l.id) AS rn_phone,
            row_number() OVER (PARTITION BY l.tenant_id, lower(l.email)
                               ORDER BY l.created_at, l.id) AS rn_email
        FROM public.leads l
    )
    INSERT INTO public.crm_leads
        (id, tenant_id, lead_no, full_name, company_name, phone, email, source,
         stage_code, priority, insurance_type, insurer_company_id,
         expected_premium, assigned_to, notes, lost_reason,
         converted_corporate_client_id, converted_at, next_followup_at,
         created_by, created_at, updated_at)
    SELECT r.id, r.tenant_id, r.lead_no,
        COALESCE(NULLIF(btrim(r.contact_name), ''), r.company_name),
        r.company_name,
        CASE WHEN r.mobile IS NULL OR r.rn_phone > 1 OR EXISTS (
            SELECT 1 FROM public.crm_leads c
            WHERE c.tenant_id = r.tenant_id AND c.phone = r.mobile)
            THEN NULL ELSE r.mobile END,
        CASE WHEN r.email IS NULL OR r.rn_email > 1 OR EXISTS (
            SELECT 1 FROM public.crm_leads c
            WHERE c.tenant_id = r.tenant_id AND lower(c.email) = lower(r.email))
            THEN NULL ELSE lower(r.email) END,
        r.source,
        CASE WHEN EXISTS (
            SELECT 1 FROM public.crm_pipeline_stages s
            WHERE s.tenant_id = r.tenant_id AND s.code = r.stage_code)
            THEN r.stage_code ELSE 'new' END,
        'normal', r.insurance_type, r.insurer_company_id,
        r.expected_premium, r.assigned_staff_id, r.notes, r.lost_reason,
        r.converted_corporate_client_id, r.converted_at, r.next_followup_at,
        r.created_by, r.created_at, r.updated_at
    FROM ranked r
    ON CONFLICT (id) DO NOTHING
    """,
    """
    UPDATE public.crm_leads c
    SET closed_at = COALESCE(c.converted_at, c.updated_at)
    WHERE c.closed_at IS NULL AND EXISTS (
        SELECT 1 FROM public.crm_pipeline_stages s
        WHERE s.tenant_id = c.tenant_id AND s.code = c.stage_code
          AND (s.is_won OR s.is_lost OR s.is_converted))
    """,
    "ALTER TABLE public.crm_leads ADD CONSTRAINT crm_leads_lead_no_key UNIQUE (lead_no)",
    "ALTER TABLE public.crm_leads ALTER COLUMN lead_no SET NOT NULL",
    "ALTER TABLE public.crm_leads DROP COLUMN status",
    "CREATE INDEX ix_crm_leads_stage ON public.crm_leads (tenant_id, stage_code)",
    "CREATE INDEX ix_crm_leads_followup ON public.crm_leads (assigned_to, next_followup_at)",
    "CREATE INDEX ix_crm_leads_insurer ON public.crm_leads (insurer_company_id)",
    # --- crm_activities: tenant + task link + widened type set ---------------
    f"""
    ALTER TABLE public.crm_activities
        ADD COLUMN tenant_id uuid NOT NULL DEFAULT {_TENANT}
            REFERENCES public.tenants(id),
        ADD COLUMN task_id uuid REFERENCES public.crm_tasks(id) ON DELETE SET NULL
    """,
    """
    UPDATE public.crm_activities a SET tenant_id = l.tenant_id
    FROM public.crm_leads l WHERE l.id = a.lead_id
    """,
    "ALTER TABLE public.crm_activities DROP CONSTRAINT crm_activities_type_check",
    "UPDATE public.crm_activities SET type = 'stage_change' WHERE type = 'status_change'",
    """
    ALTER TABLE public.crm_activities ADD CONSTRAINT crm_activities_type_check
        CHECK (type IN ('call', 'note', 'stage_change', 'task', 'conversion',
                        'whatsapp', 'email', 'meeting', 'assignment'))
    """,
    f"""
    ALTER TABLE public.crm_import_errors
        ADD COLUMN tenant_id uuid NOT NULL DEFAULT {_TENANT}
            REFERENCES public.tenants(id)
    """,
    """
    UPDATE public.crm_import_errors e SET tenant_id = b.tenant_id
    FROM public.crm_import_batches b WHERE b.id = e.batch_id
    """,
    # Timeline + schedulable activities from the old sales pipeline.
    """
    INSERT INTO public.crm_activities
        (id, tenant_id, lead_id, type, description, created_by, created_at)
    SELECT la.id, la.tenant_id, la.lead_id,
        CASE la.kind WHEN 'appointment' THEN 'meeting' ELSE la.kind END,
        la.title
            || CASE WHEN la.body IS NOT NULL AND la.body <> ''
                    THEN ' — ' || la.body ELSE '' END
            || CASE WHEN la.outcome IS NOT NULL AND la.outcome <> ''
                    THEN ' (outcome: ' || la.outcome || ')' ELSE '' END,
        la.created_by, la.created_at
    FROM public.lead_activities la
    ON CONFLICT (id) DO NOTHING
    """,
    """
    INSERT INTO public.crm_tasks
        (tenant_id, lead_id, type, title, description, due_at, status,
         assigned_to, completed_at, created_by, created_at, updated_at)
    SELECT la.tenant_id, la.lead_id,
        CASE la.kind WHEN 'appointment' THEN 'meeting'
                     WHEN 'note' THEN 'task' ELSE la.kind END,
        la.title, la.body, COALESCE(la.due_at, la.created_at),
        CASE WHEN la.done_at IS NULL THEN 'pending' ELSE 'done' END,
        l.assigned_to, la.done_at, la.created_by, la.created_at, la.created_at
    FROM public.lead_activities la
    JOIN public.crm_leads l ON l.id = la.lead_id
    WHERE la.kind IN ('task', 'appointment') OR la.due_at IS NOT NULL
    """,
    # issuing_requests now points at the unified lead.
    """
    ALTER TABLE public.issuing_requests
        DROP CONSTRAINT issuing_requests_lead_id_fkey,
        ADD CONSTRAINT issuing_requests_lead_id_fkey
            FOREIGN KEY (lead_id) REFERENCES public.crm_leads(id)
            ON DELETE SET NULL
    """,
    # One-click conversion reads crm_leads.
    """
    CREATE OR REPLACE FUNCTION app_security.convert_lead_to_client(
        p_lead_id uuid, p_slug text, p_name_ar text, p_name_en text
    ) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    DECLARE
        v_lead public.crm_leads%ROWTYPE;
        v_client uuid;
    BEGIN
        IF NOT app_security.has_permission('crm.convert') THEN
            RAISE EXCEPTION 'lead conversion forbidden' USING ERRCODE = '42501';
        END IF;
        SELECT * INTO v_lead FROM public.crm_leads WHERE id = p_lead_id FOR UPDATE;
        IF NOT FOUND OR v_lead.converted_corporate_client_id IS NOT NULL THEN
            RAISE EXCEPTION 'lead not convertible' USING ERRCODE = 'P0002';
        END IF;
        INSERT INTO public.corporate_clients(
            slug, name_ar, name_en, contact_name, contact_email, contact_mobile, tenant_id)
        VALUES (lower(btrim(p_slug)), btrim(p_name_ar), btrim(p_name_en),
            v_lead.full_name, v_lead.email, v_lead.phone, v_lead.tenant_id)
        RETURNING id INTO v_client;
        UPDATE public.crm_leads
        SET converted_corporate_client_id = v_client, converted_at = now(),
            stage_code = 'converted', closed_at = COALESCE(closed_at, now()),
            updated_at = now()
        WHERE id = p_lead_id;
        RETURN v_client;
    END;
    $function$
    """,
    # --- RLS + grants for the new tables (and the Celery reminder worker) ----
    "ALTER TABLE public.crm_pipeline_stages ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.crm_pipeline_stages FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY crm_pipeline_stages_staff ON public.crm_pipeline_stages TO app_runtime
        USING (app_security.actor_scope() = 'staff')
        WITH CHECK (app_security.actor_scope() = 'staff')
    """,
    "ALTER TABLE public.crm_tasks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.crm_tasks FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY crm_tasks_staff ON public.crm_tasks TO app_runtime
        USING (app_security.actor_scope() = 'staff')
        WITH CHECK (app_security.actor_scope() = 'staff')
    """,
    """
    CREATE POLICY crm_tasks_worker ON public.crm_tasks TO app_worker
        USING (app_security.actor_scope() = 'worker')
        WITH CHECK (app_security.actor_scope() = 'worker')
    """,
    """
    CREATE POLICY crm_leads_worker ON public.crm_leads TO app_worker
        USING (app_security.actor_scope() = 'worker')
    """,
    # DELETE: stage/task removal is an explicit, audit-logged CRM product
    # operation (stages holding leads are refused in the API with 409).
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
        ON public.crm_pipeline_stages, public.crm_tasks TO app_runtime
    """,
    "GRANT SELECT, UPDATE ON public.crm_tasks TO app_worker",
    "GRANT SELECT ON public.crm_leads TO app_worker",
    # --- contract: the split sales tables are gone ----------------------------
    "DROP TABLE public.lead_activities",
    "DROP TABLE public.leads",
    "DROP TABLE public.pipeline_stages",
)

_DOWNGRADE = (
    # Recreate the revision-039 sales tables.
    f"""
    CREATE TABLE public.pipeline_stages (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL DEFAULT {_TENANT}
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
    )
    """,
    """
    INSERT INTO public.pipeline_stages
        (id, tenant_id, code, name_ar, name_en, sort_order, is_won, is_lost,
         is_converted, is_active, created_at, updated_at)
    SELECT id, tenant_id, code, name_ar, name_en, sort_order, is_won, is_lost,
           is_converted, is_active, created_at, updated_at
    FROM public.crm_pipeline_stages
    """,
    f"""
    CREATE TABLE public.leads (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL DEFAULT {_TENANT}
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
    )
    """,
    "CREATE INDEX ix_leads_tenant_stage ON public.leads (tenant_id, stage_code)",
    "CREATE INDEX ix_leads_assigned_followup ON public.leads (assigned_staff_id, next_followup_at)",
    """
    INSERT INTO public.leads
        (id, tenant_id, lead_no, company_name, contact_name, mobile, email,
         source, insurance_type, insurer_company_id, stage_code,
         assigned_staff_id, expected_premium, notes, lost_reason,
         converted_corporate_client_id, converted_at, next_followup_at,
         created_by, created_at, updated_at)
    SELECT id, tenant_id, lead_no, COALESCE(company_name, full_name), full_name,
        phone, email,
        CASE WHEN source IN ('manual', 'whatsapp', 'call', 'email', 'portal')
             THEN source ELSE 'manual' END,
        insurance_type, insurer_company_id, stage_code,
        assigned_to, expected_premium, notes, lost_reason,
        converted_corporate_client_id, converted_at, next_followup_at,
        created_by, created_at, updated_at
    FROM public.crm_leads
    """,
    f"""
    CREATE TABLE public.lead_activities (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL DEFAULT {_TENANT}
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
    )
    """,
    "CREATE INDEX ix_lead_activities_lead ON public.lead_activities (lead_id, created_at DESC)",
    """
    INSERT INTO public.lead_activities
        (id, tenant_id, lead_id, kind, title, body, created_by, created_at)
    SELECT id, tenant_id, lead_id,
        CASE WHEN type IN ('call', 'meeting', 'note', 'task') THEN type ELSE 'note' END,
        left(description, 200), description, created_by, created_at
    FROM public.crm_activities
    """,
    """
    INSERT INTO public.lead_activities
        (tenant_id, lead_id, kind, title, body, due_at, done_at, created_by, created_at)
    SELECT tenant_id, lead_id,
        CASE WHEN type IN ('call', 'meeting') THEN type ELSE 'task' END,
        title, description, due_at, completed_at, created_by, created_at
    FROM public.crm_tasks
    """,
    """
    ALTER TABLE public.issuing_requests
        DROP CONSTRAINT issuing_requests_lead_id_fkey,
        ADD CONSTRAINT issuing_requests_lead_id_fkey
            FOREIGN KEY (lead_id) REFERENCES public.leads(id)
    """,
    """
    CREATE OR REPLACE FUNCTION app_security.convert_lead_to_client(
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
    $function$
    """,
    "ALTER TABLE public.pipeline_stages ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.pipeline_stages FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY pipeline_stages_staff ON public.pipeline_stages TO app_runtime
        USING (app_security.actor_scope() = 'staff')
        WITH CHECK (app_security.actor_scope() = 'staff')
    """,
    "ALTER TABLE public.leads ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.leads FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY leads_staff ON public.leads TO app_runtime
        USING (app_security.actor_scope() = 'staff')
        WITH CHECK (app_security.actor_scope() = 'staff')
    """,
    """
    CREATE POLICY leads_worker ON public.leads TO app_worker
        USING (app_security.actor_scope() = 'worker')
    """,
    "ALTER TABLE public.lead_activities ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.lead_activities FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY lead_activities_staff ON public.lead_activities TO app_runtime
        USING (app_security.actor_scope() = 'staff')
        WITH CHECK (app_security.actor_scope() = 'staff')
    """,
    """
    GRANT SELECT, INSERT, UPDATE
        ON public.pipeline_stages, public.leads, public.lead_activities TO app_runtime
    """,
    "GRANT SELECT ON public.leads TO app_worker",
    # crm_leads returns to the revision-040 shape.
    """
    ALTER TABLE public.crm_leads
        ADD COLUMN status text NOT NULL DEFAULT 'new'
            CHECK (status IN ('new', 'contacted', 'qualified', 'converted', 'lost'))
    """,
    """
    UPDATE public.crm_leads SET status = CASE stage_code
        WHEN 'new' THEN 'new' WHEN 'contacted' THEN 'contacted'
        WHEN 'lost' THEN 'lost' WHEN 'converted' THEN 'converted'
        ELSE 'qualified' END
    """,
    "CREATE INDEX ix_crm_leads_status ON public.crm_leads (tenant_id, status)",
    "DROP POLICY crm_leads_worker ON public.crm_leads",
    "REVOKE SELECT ON public.crm_leads FROM app_worker",
    """
    ALTER TABLE public.crm_leads
        DROP COLUMN lead_no,
        DROP COLUMN company_name,
        DROP COLUMN stage_code,
        DROP COLUMN priority,
        DROP COLUMN insurance_type,
        DROP COLUMN insurer_company_id,
        DROP COLUMN expected_premium,
        DROP COLUMN lost_reason,
        DROP COLUMN converted_corporate_client_id,
        DROP COLUMN converted_at,
        DROP COLUMN closed_at,
        DROP COLUMN next_followup_at,
        DROP COLUMN created_by
    """,
    "ALTER TABLE public.crm_activities DROP CONSTRAINT crm_activities_type_check",
    "UPDATE public.crm_activities SET type = 'note' WHERE type IN ('task', 'conversion')",
    "UPDATE public.crm_activities SET type = 'status_change' WHERE type = 'stage_change'",
    """
    ALTER TABLE public.crm_activities ADD CONSTRAINT crm_activities_type_check
        CHECK (type IN ('call', 'note', 'status_change', 'whatsapp', 'email',
                        'meeting', 'assignment'))
    """,
    "ALTER TABLE public.crm_activities DROP COLUMN task_id, DROP COLUMN tenant_id",
    "ALTER TABLE public.crm_import_errors DROP COLUMN tenant_id",
    "DROP TABLE public.crm_tasks",
    "DROP TABLE public.crm_pipeline_stages",
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE:
        op.execute(statement)
