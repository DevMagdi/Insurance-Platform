-- Revision 036: requests (not complaints), omni-channel intake and the
-- corporate client portal.
--
-- The brokerage receives *requests* from the employees of companies that hold
-- a contract with it. Most arrive on WhatsApp with attachments, some by
-- telephone call, rarely by e-mail. A request needs only an insurance policy
-- number OR a medical card number, the caller's mobile and their name, and
-- staff filter the queue by mobile number.
--
-- Three capabilities are added here:
--
--   1. Request shape      - card_number, optional policy number, intake source,
--                           the originating corporate client, and a ticket
--                           series that reads REQ- instead of CMP-.
--   2. Channel intake     - a durable intake inbox plus SECURITY DEFINER
--                           helpers that turn an inbound WhatsApp/e-mail/call
--                           event into a real request (creating or matching the
--                           customer by mobile), carrying attachments and call
--                           recordings.
--   3. Corporate portal   - contracted companies, their contracts (value,
--                           payment frequency, renewal date), contract
--                           documents, and username/password logins that see
--                           ONLY their own contracts, policies and their
--                           employees' requests.
--
-- Everything is expand-only. No existing column is dropped and no existing
-- row is rewritten, so the revision is safe to apply while the API is serving.

-- ===========================================================================
-- 1. Request shape
-- ===========================================================================

-- A request is identified by a policy number OR a medical card number. The old
-- constraint demanded a non-empty policy number on every row.
ALTER TABLE public.complaints ALTER COLUMN policy_number DROP NOT NULL;
ALTER TABLE public.complaints DROP CONSTRAINT ck_complaint_policy_number;
ALTER TABLE public.complaints ADD COLUMN card_number text;
ALTER TABLE public.complaints ADD CONSTRAINT ck_request_reference CHECK (
    (policy_number IS NOT NULL AND length(btrim(policy_number)) > 0)
    OR (card_number IS NOT NULL AND length(btrim(card_number)) > 0)
);
ALTER TABLE public.complaints ADD CONSTRAINT ck_request_card_number CHECK (
    card_number IS NULL
    OR (length(btrim(card_number)) BETWEEN 3 AND 60 AND card_number ~ '^[A-Za-z0-9][A-Za-z0-9/_ -]*$')
);

-- Where the request physically came from. `channel` already records the
-- transport; `intake_source` records who keyed it in, which is what the
-- brokerage reports on ("how many requests did WhatsApp bring us?").
ALTER TABLE public.complaints ADD COLUMN intake_source text NOT NULL DEFAULT 'portal'
    CHECK (intake_source IN ('portal', 'whatsapp', 'email', 'phone', 'sms', 'staff', 'corporate'));
ALTER TABLE public.complaints ADD COLUMN intake_reference text;

-- ===========================================================================
-- 2. Corporate clients: the companies contracted with the brokerage
-- ===========================================================================

CREATE TYPE public.payment_frequency AS ENUM
    ('monthly', 'quarterly', 'semi_annual', 'annual');
CREATE TYPE public.contract_status AS ENUM
    ('draft', 'active', 'suspended', 'expired', 'terminated');

CREATE TABLE public.corporate_clients (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name_ar text NOT NULL,
    name_en text NOT NULL,
    tax_id text,
    contact_name text,
    contact_email text,
    contact_mobile text,
    address text,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_corporate_slug CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$'),
    CONSTRAINT ck_corporate_name_ar CHECK (length(btrim(name_ar)) > 0),
    CONSTRAINT ck_corporate_name_en CHECK (length(btrim(name_en)) > 0),
    CONSTRAINT ck_corporate_contact_mobile
        CHECK (contact_mobile IS NULL OR contact_mobile ~ '^\+201[0-9]{9}$')
);

-- The commercial agreement between the brokerage and that company.
CREATE TABLE public.corporate_contracts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    corporate_client_id uuid NOT NULL
        REFERENCES public.corporate_clients(id) ON DELETE CASCADE,
    contract_no text NOT NULL UNIQUE,
    contract_value numeric(14, 2) NOT NULL,
    currency char(3) NOT NULL DEFAULT 'EGP',
    payment_frequency public.payment_frequency NOT NULL,
    starts_on date NOT NULL,
    renews_on date NOT NULL,
    status public.contract_status NOT NULL DEFAULT 'active',
    headcount integer,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_contract_value CHECK (contract_value >= 0),
    CONSTRAINT ck_contract_currency CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT ck_contract_window CHECK (renews_on >= starts_on),
    CONSTRAINT ck_contract_headcount CHECK (headcount IS NULL OR headcount >= 0),
    CONSTRAINT ck_contract_no CHECK (length(btrim(contract_no)) BETWEEN 3 AND 60)
);
CREATE INDEX ix_corporate_contracts_client
    ON public.corporate_contracts (corporate_client_id, renews_on);
CREATE INDEX ix_corporate_contracts_renewal
    ON public.corporate_contracts (renews_on) WHERE status = 'active';

-- Signed PDFs, schedules of benefits and policy copies. Content is AES-GCM
-- ciphertext in the database, exactly like complaint attachments: this
-- platform has no object store.
CREATE TABLE public.corporate_contract_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id uuid NOT NULL
        REFERENCES public.corporate_contracts(id) ON DELETE CASCADE,
    title text NOT NULL,
    doc_kind text NOT NULL DEFAULT 'contract'
        CHECK (doc_kind IN ('contract', 'policy', 'endorsement', 'invoice', 'other')),
    original_filename text NOT NULL,
    mime_type text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    checksum_sha256 text NOT NULL,
    content bytea,
    scan_status public.attachment_scan_status NOT NULL DEFAULT 'pending',
    visible_to_client boolean NOT NULL DEFAULT true,
    uploaded_by_staff_id uuid REFERENCES public.staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_contract_doc_title CHECK (length(btrim(title)) > 0)
);
CREATE INDEX ix_contract_documents_contract
    ON public.corporate_contract_documents (contract_id, created_at DESC);

-- Username/password logins for the contracted company's HR or admin staff.
CREATE TABLE public.corporate_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    corporate_client_id uuid NOT NULL
        REFERENCES public.corporate_clients(id) ON DELETE CASCADE,
    username text NOT NULL UNIQUE,
    full_name text NOT NULL,
    email text,
    password_hash text NOT NULL,
    status public.staff_status NOT NULL DEFAULT 'active',
    must_change_password boolean NOT NULL DEFAULT true,
    token_version integer NOT NULL DEFAULT 0 CHECK (token_version >= 0),
    last_login_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_corporate_username CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,39}$'),
    CONSTRAINT ck_corporate_user_name CHECK (length(btrim(full_name)) > 0)
);
CREATE INDEX ix_corporate_users_client ON public.corporate_users (corporate_client_id);

-- An employee of a contracted company is an ordinary customer with a link
-- back to their employer, so every existing complaint/request feature keeps
-- working unchanged.
ALTER TABLE public.customers
    ADD COLUMN corporate_client_id uuid REFERENCES public.corporate_clients(id);
ALTER TABLE public.customers ADD COLUMN employee_code text;
-- Index builds on pre-existing hot tables are deferred to section 17, which
-- runs CONCURRENTLY outside the migration transaction.

-- Denormalised onto the request so the corporate RLS predicate is a plain
-- column comparison instead of a per-row join.
ALTER TABLE public.complaints
    ADD COLUMN corporate_client_id uuid REFERENCES public.corporate_clients(id);
ALTER TABLE public.policies
    ADD COLUMN corporate_client_id uuid REFERENCES public.corporate_clients(id);
ALTER TABLE public.policies ADD COLUMN card_number text;
-- Corporate principals get their own refresh-token family.
ALTER TABLE public.auth_sessions DROP CONSTRAINT auth_sessions_subject_kind_check;
ALTER TABLE public.auth_sessions ADD CONSTRAINT auth_sessions_subject_kind_check
    CHECK (subject_kind IN ('staff', 'customer', 'corporate'));

-- ===========================================================================
-- 3. Identity by mobile number
-- ===========================================================================
-- A WhatsApp message or a phone call carries a mobile number and a name, never
-- a 14-digit national ID. The national ID stays encrypted and unique when it is
-- known; it is no longer the only way to exist in the system.
ALTER TABLE public.customers ALTER COLUMN national_id_ciphertext DROP NOT NULL;
ALTER TABLE public.customers ALTER COLUMN national_id_blind_index DROP NOT NULL;
ALTER TABLE public.customers DROP CONSTRAINT ck_customer_blind_index;
ALTER TABLE public.customers ADD CONSTRAINT ck_customer_blind_index
    CHECK (national_id_blind_index IS NULL OR octet_length(national_id_blind_index) = 32);

-- One customer per mobile number is what makes "filter by mobile" meaningful.
-- A database that already carries duplicates (for example the synthetic demo
-- seed) keeps a non-unique index instead of failing the upgrade.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.customers
               GROUP BY mobile HAVING count(*) > 1) THEN
        RAISE WARNING
            'customers.mobile holds duplicates, so only a non-unique lookup index '
            'is created. De-duplicate and then run: CREATE UNIQUE INDEX '
            'CONCURRENTLY uq_customers_mobile ON public.customers (mobile);';
    END IF;
END
$$;

-- ===========================================================================
-- 4. Request ticket series
-- ===========================================================================
-- New tickets read REQ-2026-000123. Existing CMP- numbers stay valid for ever.
ALTER TABLE public.complaint_keys DROP CONSTRAINT ck_ticket_no;
ALTER TABLE public.complaint_keys ADD CONSTRAINT ck_ticket_no
    CHECK (ticket_no ~ '^(REQ|CMP)-[0-9]{4}-[0-9]{6,}$');

CREATE OR REPLACE FUNCTION app_security.issue_ticket_number(p_created_at timestamptz)
RETURNS text
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT 'REQ-' || to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY') || '-'
        || lpad(seq.n::text, GREATEST(6, length(seq.n::text)), '0')
    FROM (SELECT nextval('public.complaint_ticket_sequence') AS n) AS seq
$function$;

-- ===========================================================================
-- 5. Corporate access helpers and RLS
-- ===========================================================================

CREATE FUNCTION app_security.is_active_corporate_user() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.actor_scope() = 'corporate' AND EXISTS (
        SELECT 1 FROM public.corporate_users u
        JOIN public.corporate_clients c ON c.id = u.corporate_client_id
        WHERE u.id = app_security.actor_id() AND u.status = 'active' AND c.is_active
    )
$function$;

-- The only way a corporate principal learns which company it belongs to. It
-- can never be supplied by a request payload.
CREATE FUNCTION app_security.corporate_client_id() RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.corporate_client_id FROM public.corporate_users u
    JOIN public.corporate_clients c ON c.id = u.corporate_client_id
    WHERE u.id = app_security.actor_id()
      AND app_security.actor_scope() = 'corporate'
      AND u.status = 'active' AND c.is_active
$function$;

-- Staff reach corporate records through a permission; a corporate principal
-- reaches exactly one company.
CREATE FUNCTION app_security.can_read_corporate_client(p_client_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE app_security.actor_scope()
        WHEN 'corporate' THEN p_client_id = app_security.corporate_client_id()
        WHEN 'staff' THEN app_security.has_permission('corporate.view')
        ELSE false
    END
$function$;

-- Extend request visibility with the corporate branch. A corporate principal
-- sees the requests of its own employees and nothing else.
CREATE OR REPLACE FUNCTION app_security.can_read_complaint(
    p_company_id uuid, p_insurance_type insurance_type, p_agent_id uuid,
    p_team_id uuid, p_customer_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE app_security.actor_scope()
        WHEN 'customer' THEN COALESCE(app_security.actor_id() = p_customer_id, false)
        WHEN 'corporate' THEN COALESCE((
            SELECT cu.corporate_client_id = app_security.corporate_client_id()
            FROM public.customers cu WHERE cu.id = p_customer_id
        ), false)
        WHEN 'staff' THEN app_security.has_permission('complaints.view')
            AND app_security.staff_scope_matches(
                p_company_id, p_insurance_type, p_agent_id, p_team_id
            )
        ELSE false
    END
$function$;

-- Statement-level (InitPlan) mode used by the fast complaints_read policy.
CREATE OR REPLACE FUNCTION app_security.rls_complaint_mode() RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() = 'customer' THEN
        RETURN 'customer';
    END IF;
    IF app_security.actor_scope() = 'corporate' THEN
        RETURN CASE WHEN app_security.corporate_client_id() IS NULL
            THEN 'none' ELSE 'corporate' END;
    END IF;
    IF app_security.actor_scope() <> 'staff' OR app_security.actor_scope() IS NULL
       OR NOT COALESCE(app_security.has_permission('complaints.view'), false) THEN
        RETURN 'none';
    END IF;
    IF COALESCE(app_security.has_global_scope(), false) THEN
        RETURN 'global';
    END IF;
    RETURN 'scoped';
END
$function$;

DROP POLICY complaints_read ON public.complaints;
CREATE POLICY complaints_read ON public.complaints FOR SELECT TO app_runtime USING (
    CASE (SELECT app_security.rls_complaint_mode())
        WHEN 'customer' THEN customer_id = (SELECT app_security.actor_id())
        WHEN 'corporate' THEN corporate_client_id = (SELECT app_security.corporate_client_id())
        WHEN 'global' THEN true
        WHEN 'scoped' THEN
            ((SELECT app_security.rls_scope_companies()) IS NULL
                OR company_id = ANY ((SELECT app_security.rls_scope_companies())::uuid[]))
            AND ((SELECT app_security.rls_scope_types()) IS NULL
                OR insurance_type = ANY ((SELECT app_security.rls_scope_types())::public.insurance_type[]))
            AND CASE (SELECT app_security.rls_scope_level())
                WHEN 'all' THEN true
                WHEN 'own' THEN assigned_agent_id = (SELECT app_security.actor_id())
                WHEN 'team' THEN team_id = ANY ((SELECT app_security.rls_scope_teams())::uuid[])
                ELSE false
            END
        ELSE false
    END
);

-- Child tables resolve through the parent row, so the corporate branch lands
-- on the denormalised column and cannot drift from the parent policy.
CREATE OR REPLACE FUNCTION app_security.can_read_complaint_id(
    p_complaint_id uuid, p_created_at timestamptz
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE((
        SELECT CASE
            WHEN app_security.actor_scope() = 'corporate'
                THEN c.corporate_client_id IS NOT NULL
                     AND c.corporate_client_id = app_security.corporate_client_id()
            ELSE app_security.can_read_complaint(
                c.company_id, c.insurance_type, c.assigned_agent_id, c.team_id, c.customer_id
            )
        END
        FROM public.complaints c
        WHERE c.id = p_complaint_id AND c.created_at = p_created_at
    ), false)
$function$;

-- A corporate principal may read its own employees' contact rows (that is the
-- "grouped in one place" view) but never the national-ID ciphertext, which has
-- no SELECT grant for app_runtime at all.
CREATE OR REPLACE FUNCTION app_security.can_read_customer(p_customer_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE app_security.actor_scope()
        WHEN 'customer' THEN COALESCE(app_security.actor_id() = p_customer_id, false)
        WHEN 'corporate' THEN COALESCE((
            SELECT cu.corporate_client_id = app_security.corporate_client_id()
            FROM public.customers cu WHERE cu.id = p_customer_id
        ), false)
        WHEN 'staff' THEN app_security.has_permission('complaints.view') AND EXISTS (
            SELECT 1 FROM public.complaints c
            WHERE c.customer_id = p_customer_id
              AND app_security.can_read_complaint(
                  c.company_id, c.insurance_type, c.assigned_agent_id, c.team_id, c.customer_id
              )
        )
        ELSE false
    END
$function$;

ALTER TABLE public.corporate_clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corporate_clients FORCE ROW LEVEL SECURITY;
CREATE POLICY corporate_clients_read ON public.corporate_clients
    FOR SELECT TO app_runtime USING (app_security.can_read_corporate_client(id));

ALTER TABLE public.corporate_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corporate_contracts FORCE ROW LEVEL SECURITY;
CREATE POLICY corporate_contracts_read ON public.corporate_contracts
    FOR SELECT TO app_runtime
    USING (app_security.can_read_corporate_client(corporate_client_id));

ALTER TABLE public.corporate_contract_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corporate_contract_documents FORCE ROW LEVEL SECURITY;
-- Clients only ever see documents explicitly published to them, and never an
-- unscanned binary. Staff see the full inventory.
CREATE POLICY corporate_contract_documents_read ON public.corporate_contract_documents
    FOR SELECT TO app_runtime USING (
        EXISTS (
            SELECT 1 FROM public.corporate_contracts k
            WHERE k.id = contract_id
              AND app_security.can_read_corporate_client(k.corporate_client_id)
        )
        AND (app_security.actor_scope() = 'staff'
             OR (visible_to_client AND scan_status = 'clean'))
    );

ALTER TABLE public.corporate_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.corporate_users FORCE ROW LEVEL SECURITY;
CREATE POLICY corporate_users_read ON public.corporate_users
    FOR SELECT TO app_runtime USING (
        (app_security.actor_scope() = 'corporate' AND id = app_security.actor_id())
        OR (app_security.actor_scope() = 'staff'
            AND app_security.has_permission('corporate.manage'))
    );

-- The customer row itself: add the corporate branch to the existing policy.
DROP POLICY customers_read ON public.customers;
CREATE POLICY customers_read ON public.customers FOR SELECT TO app_runtime
    USING (app_security.can_read_customer(id));

-- ===========================================================================
-- 6. Channel intake inbox
-- ===========================================================================

CREATE TYPE public.intake_state AS ENUM
    ('pending', 'created', 'linked', 'ignored', 'failed');

CREATE TABLE public.request_intake_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES public.insurance_companies(id),
    channel public.communication_channel NOT NULL,
    external_event_id text NOT NULL,
    sender text NOT NULL,
    sender_name text,
    body text NOT NULL DEFAULT '',
    media_count integer NOT NULL DEFAULT 0 CHECK (media_count >= 0),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state public.intake_state NOT NULL DEFAULT 'pending',
    complaint_id uuid,
    complaint_created_at timestamptz,
    error_code text,
    received_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    CONSTRAINT uq_intake_event UNIQUE (channel, external_event_id),
    CONSTRAINT ck_intake_sender CHECK (length(btrim(sender)) > 0)
);
CREATE INDEX ix_intake_events_state
    ON public.request_intake_events (state, received_at DESC);
CREATE INDEX ix_intake_events_sender
    ON public.request_intake_events (sender, received_at DESC);

ALTER TABLE public.request_intake_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.request_intake_events FORCE ROW LEVEL SECURITY;
CREATE POLICY request_intake_events_read ON public.request_intake_events
    FOR SELECT TO app_runtime
    USING (app_security.has_permission('requests.intake'));

-- ---------------------------------------------------------------------------
-- Normalise an Egyptian mobile number from whatever a provider sends:
-- 201012345678, +201012345678, 01012345678, 1012345678 -> +201012345678.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_security.normalize_eg_mobile(p_value text) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $function$
DECLARE digits text;
BEGIN
    IF p_value IS NULL THEN RETURN NULL; END IF;
    digits := regexp_replace(p_value, '\D', '', 'g');
    IF digits ~ '^00201[0-9]{9}$' THEN digits := substring(digits from 3);
    END IF;
    IF digits ~ '^201[0-9]{9}$' THEN RETURN '+' || digits; END IF;
    IF digits ~ '^01[0-9]{9}$' THEN RETURN '+2' || digits; END IF;
    IF digits ~ '^1[0-9]{9}$' THEN RETURN '+20' || digits; END IF;
    RETURN NULL;
END;
$function$;

-- ---------------------------------------------------------------------------
-- Turn an inbound channel event into a real request.
--
-- Callable by the worker (queued WhatsApp/e-mail events) and by authorised
-- staff (a telephone call keyed into the console). The customer is matched on
-- mobile number and created when unknown - no national ID required. The
-- request is attributed to the employer when that mobile belongs to a
-- contracted company's employee.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_security.create_channel_request(
    p_company_id uuid,
    p_channel public.communication_channel,
    p_intake_source text,
    p_mobile text,
    p_full_name text,
    p_subject text,
    p_description text,
    p_policy_number text DEFAULT NULL,
    p_card_number text DEFAULT NULL,
    p_insurance_type public.insurance_type DEFAULT 'health',
    p_intake_reference text DEFAULT NULL,
    p_corporate_client_id uuid DEFAULT NULL
) RETURNS TABLE (
    complaint_id uuid, complaint_created_at timestamptz, ticket_no text, customer_id uuid
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_mobile text;
    v_customer uuid;
    v_corporate uuid;
    v_ticket text;
    v_id uuid := gen_random_uuid();
    v_now timestamptz := now();
    v_name text;
    v_subject text;
BEGIN
    -- Only the queue worker or a staff member holding requests.intake.
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'request intake forbidden' USING ERRCODE = '42501';
    END IF;

    IF p_intake_source NOT IN ('whatsapp', 'email', 'phone', 'sms', 'staff', 'corporate') THEN
        RAISE EXCEPTION 'unsupported intake source' USING ERRCODE = '22023';
    END IF;

    v_mobile := app_security.normalize_eg_mobile(p_mobile);
    IF v_mobile IS NULL THEN
        RAISE EXCEPTION 'intake mobile invalid' USING ERRCODE = '22023';
    END IF;

    -- A request needs a policy number or a card number, nothing more.
    IF COALESCE(length(btrim(p_policy_number)), 0) = 0
       AND COALESCE(length(btrim(p_card_number)), 0) = 0 THEN
        RAISE EXCEPTION 'request needs a policy or card number' USING ERRCODE = '22023';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM public.insurance_companies
                   WHERE id = p_company_id AND is_active
                     AND p_insurance_type = ANY(supported_types)) THEN
        RAISE EXCEPTION 'company unavailable' USING ERRCODE = '23514';
    END IF;

    v_name := NULLIF(btrim(COALESCE(p_full_name, '')), '');
    IF v_name IS NULL THEN
        v_name := 'عميل ' || right(v_mobile, 4);
    END IF;

    SELECT id, corporate_client_id INTO v_customer, v_corporate
    FROM public.customers WHERE mobile = v_mobile
    ORDER BY created_at LIMIT 1;

    IF v_customer IS NULL THEN
        INSERT INTO public.customers(full_name, mobile, whatsapp_available,
                                     corporate_client_id)
        VALUES (left(v_name, 160), v_mobile, p_channel = 'whatsapp', p_corporate_client_id)
        RETURNING id, corporate_client_id INTO v_customer, v_corporate;
    ELSE
        -- Keep the directory fresh without ever overwriting a known identity.
        UPDATE public.customers SET
            whatsapp_available = whatsapp_available OR p_channel = 'whatsapp',
            corporate_client_id = COALESCE(corporate_client_id, p_corporate_client_id),
            updated_at = now()
        WHERE id = v_customer
        RETURNING corporate_client_id INTO v_corporate;
    END IF;

    v_subject := NULLIF(btrim(COALESCE(p_subject, '')), '');
    IF v_subject IS NULL THEN
        v_subject := left(NULLIF(btrim(COALESCE(p_description, '')), ''), 120);
    END IF;
    IF v_subject IS NULL THEN
        v_subject := 'طلب وارد عبر ' || p_intake_source;
    END IF;

    v_ticket := app_security.issue_ticket_number(v_now);
    INSERT INTO public.complaint_keys(id, created_at, ticket_no)
    VALUES (v_id, v_now, v_ticket);

    INSERT INTO public.complaints(
        id, created_at, ticket_no, customer_id, company_id, insurance_type,
        policy_number, card_number, subject, description, channel,
        intake_source, intake_reference, corporate_client_id)
    VALUES (
        v_id, v_now, v_ticket, v_customer, p_company_id, p_insurance_type,
        NULLIF(btrim(COALESCE(p_policy_number, '')), ''),
        NULLIF(btrim(COALESCE(p_card_number, '')), ''),
        left(v_subject, 160),
        left(COALESCE(NULLIF(btrim(p_description), ''), v_subject), 10000),
        p_channel, p_intake_source, left(p_intake_reference, 256),
        COALESCE(v_corporate, p_corporate_client_id));

    INSERT INTO public.complaint_events(
        complaint_id, complaint_created_at, event_type, to_status, metadata)
    VALUES (v_id, v_now, 'created', 'new',
        jsonb_build_object('intake_source', p_intake_source,
                           'channel', p_channel::text,
                           'reference', p_intake_reference));

    RETURN QUERY SELECT v_id, v_now, v_ticket, v_customer;
END;
$function$;

-- ---------------------------------------------------------------------------
-- Record the inbound message body against the request it created, so the
-- WhatsApp thread reads as a conversation from the first message.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_security.append_intake_message(
    p_complaint_id uuid, p_created_at timestamptz,
    p_channel public.communication_channel, p_body text,
    p_sender text, p_external_message_id text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_message uuid; v_customer uuid;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'request intake forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT customer_id INTO v_customer FROM public.complaints
    WHERE id = p_complaint_id AND created_at = p_created_at;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'request not found' USING ERRCODE = 'P0002';
    END IF;
    INSERT INTO public.complaint_messages(
        complaint_id, complaint_created_at, body, visibility, channel, direction,
        sender_customer_id, sender_external, external_message_id, delivery_status)
    VALUES (p_complaint_id, p_created_at, left(COALESCE(p_body, ''), 5000), 'public',
        p_channel, 'inbound', v_customer, left(p_sender, 254),
        left(p_external_message_id, 256), 'received')
    ON CONFLICT (channel, external_message_id) WHERE external_message_id IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_message;
    RETURN v_message;
END;
$function$;

-- ---------------------------------------------------------------------------
-- Store an attachment that arrived with an inbound channel message. The binary
-- is already AES-GCM ciphertext produced by the worker; it stays `pending`
-- until the scanner clears it, exactly like a portal upload.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_security.store_intake_attachment(
    p_complaint_id uuid, p_created_at timestamptz, p_message_id uuid,
    p_filename text, p_mime_type text, p_size bigint,
    p_checksum text, p_content bytea
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'request intake forbidden' USING ERRCODE = '42501';
    END IF;
    IF p_size IS NULL OR p_size <= 0 OR p_size > 10485760
       OR octet_length(COALESCE(p_content, ''::bytea)) < 28
       OR length(COALESCE(p_checksum, '')) <> 64 THEN
        RAISE EXCEPTION 'intake attachment invalid' USING ERRCODE = '22023';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.complaints
                   WHERE id = p_complaint_id AND created_at = p_created_at) THEN
        RAISE EXCEPTION 'request not found' USING ERRCODE = 'P0002';
    END IF;
    INSERT INTO public.attachments(
        complaint_id, complaint_created_at, message_id, visibility,
        original_filename, mime_type, size_bytes, checksum_sha256,
        content, scan_status)
    VALUES (p_complaint_id, p_created_at, p_message_id, 'public',
        left(p_filename, 255), left(p_mime_type, 180), p_size, p_checksum,
        p_content, 'pending')
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

-- ---------------------------------------------------------------------------
-- Telephone intake: a call becomes a request, and the recording plus the
-- transcript are stored encrypted against it.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_security.record_request_call(
    p_complaint_id uuid, p_created_at timestamptz,
    p_direction public.message_direction, p_provider text,
    p_provider_call_id text, p_recording bytea, p_transcript bytea,
    p_outcome text, p_consent boolean, p_duration_seconds integer
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid := gen_random_uuid();
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'call intake forbidden' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.complaints
                   WHERE id = p_complaint_id AND created_at = p_created_at) THEN
        RAISE EXCEPTION 'request not found' USING ERRCODE = 'P0002';
    END IF;
    -- A recording is personal data: it is only retained with recorded consent.
    IF p_recording IS NOT NULL AND NOT COALESCE(p_consent, false) THEN
        RAISE EXCEPTION 'call recording requires consent' USING ERRCODE = '42501';
    END IF;
    IF p_recording IS NOT NULL AND octet_length(p_recording) < 28 THEN
        RAISE EXCEPTION 'call recording must be encrypted' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.call_logs(
        id, complaint_id, complaint_created_at, direction, provider,
        provider_call_id, recording_content, transcript_ciphertext, outcome,
        consent_at, status, started_at, ended_at)
    VALUES (v_id, p_complaint_id, p_created_at, p_direction, left(p_provider, 60),
        left(p_provider_call_id, 256), p_recording, p_transcript,
        left(COALESCE(p_outcome, 'completed'), 80),
        CASE WHEN COALESCE(p_consent, false) THEN now() ELSE NULL END,
        'completed', now() - make_interval(secs => GREATEST(COALESCE(p_duration_seconds, 0), 0)),
        now());
    INSERT INTO public.complaint_events(
        complaint_id, complaint_created_at, event_type, metadata)
    VALUES (p_complaint_id, p_created_at, 'inbound_voice_call',
        jsonb_build_object('call_log_id', v_id, 'recorded', p_recording IS NOT NULL,
                           'consent', COALESCE(p_consent, false),
                           'duration_seconds', GREATEST(COALESCE(p_duration_seconds, 0), 0)));
    RETURN v_id;
END;
$function$;

-- ---------------------------------------------------------------------------
-- Durable intake inbox bookkeeping.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_security.record_intake_event(
    p_company_id uuid, p_channel public.communication_channel,
    p_external_event_id text, p_sender text, p_sender_name text, p_body text,
    p_media_count integer, p_payload jsonb, p_state public.intake_state,
    p_complaint_id uuid, p_complaint_created_at timestamptz, p_error text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'request intake forbidden' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.request_intake_events(
        company_id, channel, external_event_id, sender, sender_name, body,
        media_count, payload, state, complaint_id, complaint_created_at,
        error_code, processed_at)
    VALUES (p_company_id, p_channel, left(p_external_event_id, 256),
        left(p_sender, 254), left(p_sender_name, 160), left(COALESCE(p_body, ''), 5000),
        GREATEST(COALESCE(p_media_count, 0), 0), COALESCE(p_payload, '{}'::jsonb),
        p_state, p_complaint_id, p_complaint_created_at, left(p_error, 120),
        CASE WHEN p_state = 'pending' THEN NULL ELSE now() END)
    ON CONFLICT (channel, external_event_id) DO UPDATE SET
        state = EXCLUDED.state,
        complaint_id = COALESCE(EXCLUDED.complaint_id, public.request_intake_events.complaint_id),
        complaint_created_at = COALESCE(EXCLUDED.complaint_created_at,
                                        public.request_intake_events.complaint_created_at),
        error_code = EXCLUDED.error_code,
        processed_at = COALESCE(EXCLUDED.processed_at,
                                public.request_intake_events.processed_at)
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

-- ===========================================================================
-- 7. Corporate portal read models
-- ===========================================================================

-- Everything the signed-in corporate user needs on one screen: the contract
-- commercials, the renewal date, the published documents and the policy list.
CREATE FUNCTION app_security.corporate_overview() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.corporate_client_id() IS NULL THEN NULL ELSE (
        SELECT jsonb_build_object(
            'client', jsonb_build_object(
                'id', c.id, 'slug', c.slug, 'name_ar', c.name_ar, 'name_en', c.name_en,
                'contact_name', c.contact_name, 'contact_email', c.contact_email,
                'contact_mobile', c.contact_mobile, 'address', c.address),
            'contracts', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'id', k.id, 'contract_no', k.contract_no,
                    'contract_value', k.contract_value, 'currency', k.currency,
                    'payment_frequency', k.payment_frequency::text,
                    'starts_on', k.starts_on, 'renews_on', k.renews_on,
                    'status', k.status::text, 'headcount', k.headcount,
                    'days_to_renewal', (k.renews_on - CURRENT_DATE),
                    'documents', COALESCE((
                        SELECT jsonb_agg(jsonb_build_object(
                            'id', d.id, 'title', d.title, 'doc_kind', d.doc_kind,
                            'original_filename', d.original_filename,
                            'mime_type', d.mime_type, 'size_bytes', d.size_bytes,
                            'created_at', d.created_at)
                            ORDER BY d.created_at DESC)
                        FROM public.corporate_contract_documents d
                        WHERE d.contract_id = k.id AND d.visible_to_client
                          AND d.scan_status = 'clean'), '[]'::jsonb))
                    ORDER BY k.renews_on)
                FROM public.corporate_contracts k
                WHERE k.corporate_client_id = c.id), '[]'::jsonb),
            'policies', COALESCE((
                SELECT jsonb_agg(jsonb_build_object(
                    'id', p.id, 'policy_number', p.policy_number,
                    'card_number', p.card_number,
                    'insurance_type', p.insurance_type::text,
                    'company_name_ar', ic.name_ar, 'company_name_en', ic.name_en,
                    'effective_from', p.effective_from, 'expires_at', p.expires_at,
                    'member_name', cu.full_name)
                    ORDER BY cu.full_name)
                FROM public.policies p
                JOIN public.insurance_companies ic ON ic.id = p.company_id
                LEFT JOIN public.customers cu ON cu.id = p.customer_id
                WHERE p.corporate_client_id = c.id
                   OR cu.corporate_client_id = c.id), '[]'::jsonb),
            'employees_count', (SELECT count(*) FROM public.customers e
                                WHERE e.corporate_client_id = c.id),
            'requests_total', (SELECT count(*) FROM public.complaints x
                               WHERE x.corporate_client_id = c.id),
            'requests_open', (SELECT count(*) FROM public.complaints x
                              WHERE x.corporate_client_id = c.id
                                AND x.status <> 'resolved')
        )
        FROM public.corporate_clients c WHERE c.id = app_security.corporate_client_id()
    ) END
$function$;

-- The employee directory with each member's requests grouped underneath, which
-- is what "filtered by name, linked, grouped in one place" asks for.
CREATE FUNCTION app_security.corporate_members(p_search text DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.corporate_client_id() IS NULL THEN '[]'::jsonb ELSE
        COALESCE((
        SELECT jsonb_agg(member ORDER BY member->>'full_name')
        FROM (
            SELECT jsonb_build_object(
                'id', e.id, 'full_name', e.full_name, 'mobile', e.mobile,
                'email', e.email, 'employee_code', e.employee_code,
                'requests_count', (SELECT count(*) FROM public.complaints x
                                   WHERE x.customer_id = e.id
                                     AND x.corporate_client_id = e.corporate_client_id),
                'requests', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'id', x.id, 'ticket_no', x.ticket_no, 'subject', x.subject,
                        'status', x.status::text, 'channel', x.channel::text,
                        'intake_source', x.intake_source,
                        'policy_number', x.policy_number, 'card_number', x.card_number,
                        'created_at', x.created_at, 'updated_at', x.updated_at)
                        ORDER BY x.created_at DESC)
                    FROM public.complaints x
                    WHERE x.customer_id = e.id
                      AND x.corporate_client_id = e.corporate_client_id), '[]'::jsonb)
            ) AS member
            FROM public.customers e
            WHERE e.corporate_client_id = app_security.corporate_client_id()
              AND (p_search IS NULL OR btrim(p_search) = ''
                   OR e.full_name ILIKE '%' || btrim(p_search) || '%'
                   OR e.mobile LIKE '%' || regexp_replace(btrim(p_search), '\D', '', 'g') || '%')
            LIMIT 500
        ) rows), '[]'::jsonb)
    END
$function$;

-- Staff-side administration view of every contracted company.
CREATE FUNCTION app_security.corporate_directory() RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN NOT COALESCE(app_security.has_permission('corporate.view'), false)
        THEN '[]'::jsonb ELSE COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'id', c.id, 'slug', c.slug, 'name_ar', c.name_ar, 'name_en', c.name_en,
            'tax_id', c.tax_id, 'contact_name', c.contact_name,
            'contact_email', c.contact_email, 'contact_mobile', c.contact_mobile,
            'is_active', c.is_active, 'created_at', c.created_at,
            'employees_count', (SELECT count(*) FROM public.customers e
                                WHERE e.corporate_client_id = c.id),
            'requests_count', (SELECT count(*) FROM public.complaints x
                               WHERE x.corporate_client_id = c.id),
            'users', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', u.id, 'username', u.username, 'full_name', u.full_name,
                    'email', u.email, 'status', u.status::text,
                    'last_login_at', u.last_login_at) ORDER BY u.username)
                FROM public.corporate_users u
                WHERE u.corporate_client_id = c.id), '[]'::jsonb),
            'contracts', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', k.id, 'contract_no', k.contract_no,
                    'contract_value', k.contract_value, 'currency', k.currency,
                    'payment_frequency', k.payment_frequency::text,
                    'starts_on', k.starts_on, 'renews_on', k.renews_on,
                    'status', k.status::text, 'headcount', k.headcount,
                    'days_to_renewal', (k.renews_on - CURRENT_DATE),
                    'documents_count', (SELECT count(*)
                        FROM public.corporate_contract_documents d
                        WHERE d.contract_id = k.id))
                    ORDER BY k.renews_on)
                FROM public.corporate_contracts k
                WHERE k.corporate_client_id = c.id), '[]'::jsonb)
        ) ORDER BY c.name_ar) FROM public.corporate_clients c), '[]'::jsonb) END
$function$;

-- ===========================================================================
-- 8. Corporate authentication (username + password)
-- ===========================================================================

CREATE FUNCTION app_security.corporate_login_record(p_username text)
RETURNS TABLE (id uuid, corporate_client_id uuid, full_name text,
               password_hash text, status text, must_change_password boolean,
               token_version integer, client_active boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() NOT IN ('corporate_auth', 'staff_auth') THEN
        RETURN;
    END IF;
    RETURN QUERY
    SELECT u.id, u.corporate_client_id, u.full_name, u.password_hash,
           u.status::text, u.must_change_password, u.token_version, c.is_active
    FROM public.corporate_users u
    JOIN public.corporate_clients c ON c.id = u.corporate_client_id
    WHERE u.username = lower(btrim(p_username));
END;
$function$;

CREATE FUNCTION app_security.corporate_mark_login(p_user_id uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'corporate_auth' THEN RETURN false; END IF;
    UPDATE public.corporate_users SET last_login_at = now(), updated_at = now()
    WHERE id = p_user_id AND status = 'active';
    RETURN FOUND;
END;
$function$;

CREATE FUNCTION app_security.corporate_token_version(p_user_id uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.token_version FROM public.corporate_users u
    JOIN public.corporate_clients c ON c.id = u.corporate_client_id
    WHERE u.id = p_user_id AND u.status = 'active' AND c.is_active
$function$;

CREATE FUNCTION app_security.corporate_change_password(
    p_user_id uuid, p_password_hash text
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() NOT IN ('corporate', 'corporate_auth')
       OR app_security.actor_id() IS DISTINCT FROM p_user_id THEN
        RAISE EXCEPTION 'corporate password change forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.corporate_users
    SET password_hash = p_password_hash, must_change_password = false,
        token_version = token_version + 1, updated_at = now()
    WHERE id = p_user_id AND status = 'active';
    RETURN FOUND;
END;
$function$;

-- ===========================================================================
-- 9. Staff administration of corporate clients
-- ===========================================================================

CREATE FUNCTION app_security.upsert_corporate_client(
    p_id uuid, p_slug text, p_name_ar text, p_name_en text, p_tax_id text,
    p_contact_name text, p_contact_email text, p_contact_mobile text,
    p_address text, p_is_active boolean
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('corporate.manage') THEN
        RAISE EXCEPTION 'corporate administration forbidden' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NULL THEN
        INSERT INTO public.corporate_clients(
            slug, name_ar, name_en, tax_id, contact_name, contact_email,
            contact_mobile, address, is_active)
        VALUES (lower(btrim(p_slug)), btrim(p_name_ar), btrim(p_name_en),
            NULLIF(btrim(COALESCE(p_tax_id, '')), ''),
            NULLIF(btrim(COALESCE(p_contact_name, '')), ''),
            NULLIF(btrim(COALESCE(p_contact_email, '')), ''),
            app_security.normalize_eg_mobile(p_contact_mobile),
            NULLIF(btrim(COALESCE(p_address, '')), ''), COALESCE(p_is_active, true))
        RETURNING id INTO v_id;
    ELSE
        UPDATE public.corporate_clients SET
            name_ar = btrim(p_name_ar), name_en = btrim(p_name_en),
            tax_id = NULLIF(btrim(COALESCE(p_tax_id, '')), ''),
            contact_name = NULLIF(btrim(COALESCE(p_contact_name, '')), ''),
            contact_email = NULLIF(btrim(COALESCE(p_contact_email, '')), ''),
            contact_mobile = app_security.normalize_eg_mobile(p_contact_mobile),
            address = NULLIF(btrim(COALESCE(p_address, '')), ''),
            is_active = COALESCE(p_is_active, is_active), updated_at = now()
        WHERE id = p_id RETURNING id INTO v_id;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'corporate client not found' USING ERRCODE = 'P0002';
        END IF;
    END IF;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.upsert_corporate_contract(
    p_id uuid, p_client_id uuid, p_contract_no text, p_value numeric,
    p_currency text, p_frequency public.payment_frequency, p_starts_on date,
    p_renews_on date, p_status public.contract_status, p_headcount integer,
    p_notes text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('corporate.manage') THEN
        RAISE EXCEPTION 'corporate administration forbidden' USING ERRCODE = '42501';
    END IF;
    IF p_id IS NULL THEN
        INSERT INTO public.corporate_contracts(
            corporate_client_id, contract_no, contract_value, currency,
            payment_frequency, starts_on, renews_on, status, headcount, notes)
        VALUES (p_client_id, btrim(p_contract_no), p_value,
            upper(COALESCE(NULLIF(btrim(p_currency), ''), 'EGP')), p_frequency,
            p_starts_on, p_renews_on, COALESCE(p_status, 'active'), p_headcount,
            NULLIF(btrim(COALESCE(p_notes, '')), ''))
        RETURNING id INTO v_id;
    ELSE
        UPDATE public.corporate_contracts SET
            contract_no = btrim(p_contract_no), contract_value = p_value,
            currency = upper(COALESCE(NULLIF(btrim(p_currency), ''), 'EGP')),
            payment_frequency = p_frequency, starts_on = p_starts_on,
            renews_on = p_renews_on, status = COALESCE(p_status, status),
            headcount = p_headcount,
            notes = NULLIF(btrim(COALESCE(p_notes, '')), ''), updated_at = now()
        WHERE id = p_id RETURNING id INTO v_id;
        IF v_id IS NULL THEN
            RAISE EXCEPTION 'contract not found' USING ERRCODE = 'P0002';
        END IF;
    END IF;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.create_corporate_user(
    p_client_id uuid, p_username text, p_full_name text, p_email text,
    p_password_hash text
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('corporate.manage') THEN
        RAISE EXCEPTION 'corporate administration forbidden' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.corporate_users(
        corporate_client_id, username, full_name, email, password_hash)
    VALUES (p_client_id, lower(btrim(p_username)), btrim(p_full_name),
        NULLIF(btrim(COALESCE(p_email, '')), ''), p_password_hash)
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.link_customer_to_corporate(
    p_customer_id uuid, p_client_id uuid, p_employee_code text
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.has_permission('corporate.manage') THEN
        RAISE EXCEPTION 'corporate administration forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.customers
    SET corporate_client_id = p_client_id,
        employee_code = NULLIF(btrim(COALESCE(p_employee_code, '')), ''),
        updated_at = now()
    WHERE id = p_customer_id;
    IF NOT FOUND THEN RETURN false; END IF;
    -- Back-fill the denormalised column so existing requests appear in the
    -- client portal immediately.
    UPDATE public.complaints SET corporate_client_id = p_client_id, updated_at = now()
    WHERE customer_id = p_customer_id AND corporate_client_id IS DISTINCT FROM p_client_id;
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.store_contract_document(
    p_contract_id uuid, p_title text, p_doc_kind text, p_filename text,
    p_mime_type text, p_size bigint, p_checksum text, p_content bytea,
    p_visible boolean
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF NOT app_security.has_permission('corporate.manage') THEN
        RAISE EXCEPTION 'corporate administration forbidden' USING ERRCODE = '42501';
    END IF;
    IF p_size IS NULL OR p_size <= 0 OR p_size > 10485760
       OR octet_length(COALESCE(p_content, ''::bytea)) < 28
       OR length(COALESCE(p_checksum, '')) <> 64 THEN
        RAISE EXCEPTION 'contract document invalid' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.corporate_contract_documents(
        contract_id, title, doc_kind, original_filename, mime_type, size_bytes,
        checksum_sha256, content, scan_status, visible_to_client,
        uploaded_by_staff_id)
    VALUES (p_contract_id, btrim(p_title), COALESCE(NULLIF(btrim(p_doc_kind), ''), 'contract'),
        left(p_filename, 255), left(p_mime_type, 180), p_size, p_checksum,
        p_content, 'pending', COALESCE(p_visible, true), app_security.actor_id())
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$function$;

-- Reading a stored document returns ciphertext only to a principal that may
-- see that contract; the decryption key never leaves the API process.
CREATE FUNCTION app_security.read_contract_document(p_document_id uuid)
RETURNS TABLE (content bytea, original_filename text, mime_type text,
               size_bytes bigint, scan_status text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    RETURN QUERY
    SELECT d.content, d.original_filename, d.mime_type, d.size_bytes,
           d.scan_status::text
    FROM public.corporate_contract_documents d
    JOIN public.corporate_contracts k ON k.id = d.contract_id
    WHERE d.id = p_document_id
      AND app_security.can_read_corporate_client(k.corporate_client_id)
      AND (app_security.actor_scope() = 'staff'
           OR (d.visible_to_client AND d.scan_status = 'clean'));
END;
$function$;

-- Scanner callback for contract documents, mirroring attachment scanning.
CREATE FUNCTION app_security.set_contract_document_scan(
    p_document_id uuid, p_status public.attachment_scan_status
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT ((session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
            OR app_security.has_permission('corporate.manage')) THEN
        RAISE EXCEPTION 'scan update forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.corporate_contract_documents SET scan_status = p_status
    WHERE id = p_document_id;
    RETURN FOUND;
END;
$function$;

-- ===========================================================================
-- 10. Permission catalog additions
-- ===========================================================================
INSERT INTO public.permissions (code, resource, action) VALUES
    ('requests.intake', 'requests', 'intake'),
    ('corporate.view', 'corporate', 'view'),
    ('corporate.manage', 'corporate', 'manage')
ON CONFLICT (code) DO NOTHING;

-- Every existing administrator keeps a complete catalog.
INSERT INTO public.role_permissions (role_id, permission_code)
SELECT r.id, p.code
FROM public.roles r
CROSS JOIN (VALUES ('requests.intake'), ('corporate.view'), ('corporate.manage')) AS p(code)
WHERE r.is_system AND r.name = 'administrator'
ON CONFLICT DO NOTHING;

-- ===========================================================================
-- 11. Grants
-- ===========================================================================
GRANT SELECT (card_number, intake_source, intake_reference, corporate_client_id)
    ON public.complaints TO app_runtime;
GRANT INSERT (card_number, intake_source, intake_reference, corporate_client_id)
    ON public.complaints TO app_runtime;
GRANT UPDATE (card_number, intake_source, intake_reference, corporate_client_id)
    ON public.complaints TO app_runtime;
GRANT SELECT (corporate_client_id, employee_code) ON public.customers TO app_runtime;
GRANT SELECT (corporate_client_id, card_number) ON public.policies TO app_runtime;
GRANT SELECT ON public.corporate_clients, public.corporate_contracts,
    public.request_intake_events TO app_runtime;
GRANT SELECT (id, contract_id, title, doc_kind, original_filename, mime_type,
    size_bytes, checksum_sha256, scan_status, visible_to_client,
    uploaded_by_staff_id, created_at)
    ON public.corporate_contract_documents TO app_runtime;
GRANT SELECT (id, corporate_client_id, username, full_name, email, status,
    must_change_password, token_version, last_login_at, created_at, updated_at)
    ON public.corporate_users TO app_runtime;

REVOKE ALL ON FUNCTION app_security.create_channel_request(
    uuid, public.communication_channel, text, text, text, text, text, text, text,
    public.insurance_type, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.append_intake_message(
    uuid, timestamptz, public.communication_channel, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.store_intake_attachment(
    uuid, timestamptz, uuid, text, text, bigint, text, bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.record_request_call(
    uuid, timestamptz, public.message_direction, text, text, bytea, bytea, text,
    boolean, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.record_intake_event(
    uuid, public.communication_channel, text, text, text, text, integer, jsonb,
    public.intake_state, uuid, timestamptz, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.corporate_login_record(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.corporate_mark_login(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.corporate_change_password(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.set_contract_document_scan(
    uuid, public.attachment_scan_status) FROM PUBLIC;

-- Grant the runtime role exactly the functions this migration adds for it.
--
-- `GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app_security TO app_runtime` would
-- be shorter, and migrations 002 and 005 use it — but they run before the
-- worker-only functions exist, so the blanket grant never reached those. This
-- migration runs last, so the same statement here would hand app_runtime
-- EXECUTE on worker-only entry points such as
-- `app_security.claim_integration_jobs(integer)`. The web role must not be
-- able to claim the worker's jobs, so each function is named explicitly.
GRANT EXECUTE ON FUNCTION app_security.corporate_overview() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_members(text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_directory() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_client_id() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_token_version(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.is_active_corporate_user() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.can_read_corporate_client(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_login_record(text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_mark_login(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.corporate_change_password(uuid, text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.read_contract_document(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.store_contract_document(
    uuid, text, text, text, text, bigint, text, bytea, boolean) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.upsert_corporate_client(
    uuid, text, text, text, text, text, text, text, text, boolean) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.upsert_corporate_contract(
    uuid, uuid, text, numeric, text, public.payment_frequency, date, date,
    public.contract_status, integer, text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.create_corporate_user(
    uuid, text, text, text, text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.link_customer_to_corporate(
    uuid, uuid, text) TO app_runtime;

-- Defence in depth: should a later migration reintroduce a blanket grant,
-- these worker-only entry points stay closed to the web role.
REVOKE EXECUTE ON FUNCTION app_security.claim_integration_jobs(integer) FROM app_runtime;

-- The worker owns inbound delivery: it may create requests from channel events
-- and store their binaries, and nothing else.
GRANT USAGE ON SCHEMA app_security TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.create_channel_request(
    uuid, public.communication_channel, text, text, text, text, text, text, text,
    public.insurance_type, text, uuid) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.append_intake_message(
    uuid, timestamptz, public.communication_channel, text, text, text) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.store_intake_attachment(
    uuid, timestamptz, uuid, text, text, bigint, text, bytea) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.record_request_call(
    uuid, timestamptz, public.message_direction, text, text, bytea, bytea, text,
    boolean, integer) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.record_intake_event(
    uuid, public.communication_channel, text, text, text, text, integer, jsonb,
    public.intake_state, uuid, timestamptz, text) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.set_contract_document_scan(
    uuid, public.attachment_scan_status) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.normalize_eg_mobile(text) TO app_worker;
GRANT EXECUTE ON FUNCTION app_security.issue_ticket_number(timestamptz) TO app_worker;

-- ===========================================================================
-- 12. Portal identity when no national ID is on file
-- ===========================================================================
-- The original check demanded a blind-index match, which a channel-created
-- customer does not have. Mobile alone is sufficient *only* when the signed-in
-- customer genuinely has no national ID recorded; once one exists it must
-- still match, so an existing identity can never be weakened.
CREATE OR REPLACE FUNCTION app_security.verify_customer_identity(p_mobile text, p_blind bytea)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.actor_scope() = 'customer' AND EXISTS (
        SELECT 1 FROM public.customers c WHERE c.id = app_security.actor_id()
            AND c.mobile = p_mobile
            AND (
                (p_blind IS NOT NULL AND c.national_id_blind_index = p_blind)
                OR (p_blind IS NULL AND c.national_id_blind_index IS NULL)
            ))
$function$;

-- Link a national ID to a customer that was created from a channel event, so
-- the member can later use guest tracking. Never overwrites a known identity.
CREATE FUNCTION app_security.attach_customer_national_id(
    p_customer_id uuid, p_ciphertext bytea, p_blind bytea, p_last4 text
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.has_permission('requests.intake')
       AND app_security.actor_id() IS DISTINCT FROM p_customer_id THEN
        RAISE EXCEPTION 'identity update forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.customers
    SET national_id_ciphertext = p_ciphertext,
        national_id_blind_index = p_blind,
        national_id_last4 = p_last4,
        updated_at = now()
    WHERE id = p_customer_id AND national_id_blind_index IS NULL;
    RETURN FOUND;
END;
$function$;
REVOKE ALL ON FUNCTION app_security.attach_customer_national_id(uuid, bytea, bytea, text)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.attach_customer_national_id(uuid, bytea, bytea, text)
    TO app_runtime;

-- ===========================================================================
-- 13. Session version for corporate principals
-- ===========================================================================
-- Every authenticated request re-reads the subject's token version, so a
-- password change or a disabled account invalidates live sessions at once.
CREATE OR REPLACE FUNCTION app_security.current_subject_version() RETURNS integer
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_version integer;
BEGIN
    IF app_security.actor_scope() = 'staff' THEN
        SELECT token_version INTO v_version FROM public.staff_users
        WHERE id = app_security.actor_id() AND status = 'active';
    ELSIF app_security.actor_scope() = 'customer' THEN
        SELECT token_version INTO v_version FROM public.customers
        WHERE id = app_security.actor_id();
    ELSIF app_security.actor_scope() = 'corporate' THEN
        SELECT u.token_version INTO v_version FROM public.corporate_users u
        JOIN public.corporate_clients c ON c.id = u.corporate_client_id
        WHERE u.id = app_security.actor_id() AND u.status = 'active' AND c.is_active;
    END IF;
    RETURN v_version;
END;
$function$;

-- A corporate principal owns its own refresh-token family, exactly like a
-- customer or a staff member. The session row is still written inside a
-- transaction whose GUCs the API controls, never from request data.
DROP POLICY auth_sessions_owner ON public.auth_sessions;
CREATE POLICY auth_sessions_owner ON public.auth_sessions FOR ALL TO app_runtime
    USING (subject_id = app_security.actor_id()
           AND subject_kind = app_security.actor_scope()
           AND (subject_kind = 'customer'
                OR (subject_kind = 'corporate' AND app_security.is_active_corporate_user())
                OR app_security.is_active_staff()))
    WITH CHECK (subject_id = app_security.actor_id()
           AND subject_kind = app_security.actor_scope()
           AND (subject_kind = 'customer'
                OR (subject_kind = 'corporate' AND app_security.is_active_corporate_user())
                OR app_security.is_active_staff()));

-- ============================================================================
-- Section 15: intake target lookup
-- ----------------------------------------------------------------------------
-- app_worker deliberately holds no table privileges, so the intake service
-- cannot read complaints/customers directly to decide whether an inbound
-- message continues an existing request. This definer function is the only
-- read path it needs: an explicit ticket number wins, otherwise the sender's
-- mobile is matched against recent, still-open requests.
-- ============================================================================

CREATE OR REPLACE FUNCTION app_security.find_intake_target(
    p_ticket_no text,
    p_sender text,
    p_window interval DEFAULT interval '72 hours'
)
RETURNS TABLE (complaint_id uuid, complaint_created_at timestamptz, ticket_no text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_mobile text;
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'not_authorized' USING ERRCODE = '42501';
    END IF;

    IF p_ticket_no IS NOT NULL AND length(trim(p_ticket_no)) > 0 THEN
        RETURN QUERY
        SELECT c.id, c.created_at, c.ticket_no
        FROM public.complaints c
        WHERE c.ticket_no = upper(trim(p_ticket_no))
        LIMIT 1;
        IF FOUND THEN
            RETURN;
        END IF;
    END IF;

    v_mobile := app_security.normalize_eg_mobile(p_sender);
    IF v_mobile IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT c.id, c.created_at, c.ticket_no
    FROM public.complaints c
    JOIN public.customers cu ON cu.id = c.customer_id
    WHERE cu.mobile = v_mobile
      AND c.status <> 'resolved'
      AND c.created_at >= now() - p_window
    ORDER BY c.created_at DESC
    LIMIT 1;
END;
$$;

REVOKE ALL ON FUNCTION app_security.find_intake_target(text, text, interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.find_intake_target(text, text, interval) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.find_intake_target(text, text, interval) TO app_worker;

-- ============================================================================
-- Section 16: intake replay guard
-- ----------------------------------------------------------------------------
-- Meta retries a webhook until it is acknowledged, and an IMAP poller can see
-- the same message twice after a crash. append_intake_message() and
-- store_intake_attachment() already de-duplicate on the external id, but the
-- work leading up to them (media downloads, customer matching) should not run
-- a second time either. This lets intake recognise an event it has already
-- settled and return the original outcome.
-- ============================================================================

DROP FUNCTION IF EXISTS app_security.intake_event_outcome(
    public.communication_channel, text);

CREATE OR REPLACE FUNCTION app_security.intake_event_outcome(
    p_channel public.communication_channel,
    p_external_event_id text
)
RETURNS TABLE (
    state public.intake_state,
    complaint_id uuid,
    complaint_created_at timestamptz,
    ticket_no text
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
BEGIN
    IF NOT (
        (session_user = 'app_worker' AND app_security.actor_scope() = 'worker')
        OR app_security.has_permission('requests.intake')
    ) THEN
        RAISE EXCEPTION 'not_authorized' USING ERRCODE = '42501';
    END IF;

    RETURN QUERY
    SELECT e.state, e.complaint_id, e.complaint_created_at, c.ticket_no
    FROM public.request_intake_events e
    LEFT JOIN public.complaints c ON c.id = e.complaint_id
    WHERE e.channel = p_channel
      AND e.external_event_id = p_external_event_id
      -- A previous failure must be retried, not replayed back as settled.
      AND e.state IN ('created', 'linked', 'ignored')
    LIMIT 1;
END;
$$;

REVOKE ALL ON FUNCTION app_security.intake_event_outcome(
    public.communication_channel, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.intake_event_outcome(
    public.communication_channel, text) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.intake_event_outcome(
    public.communication_channel, text) TO app_worker;

-- ============================================================================
-- Section 17: concurrent index builds (NON-TRANSACTIONAL)
-- ----------------------------------------------------------------------------
-- Everything above runs inside the migration transaction. The statements below
-- MUST NOT: customers, complaints and policies are large live tables, and a
-- plain CREATE INDEX blocks writes for the whole build. The revision replays
-- this section inside an Alembic autocommit block.
--
-- Each one is IF NOT EXISTS so a retry after an interrupted deployment is a
-- no-op. Note that a failed CONCURRENTLY build leaves an INVALID index behind:
-- drop it before re-running.
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_customers_corporate
    ON public.customers (corporate_client_id, full_name)
    WHERE corporate_client_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_customers_mobile_lookup
    ON public.customers (mobile);

-- complaints is partitioned, and PostgreSQL rejects CONCURRENTLY on a
-- partitioned parent. The revision builds that one in Python: an empty parent
-- index ON ONLY, a CONCURRENTLY build per partition, then ATTACH.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_policies_corporate
    ON public.policies (corporate_client_id)
    WHERE corporate_client_id IS NOT NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_policies_card_number
    ON public.policies (card_number)
    WHERE card_number IS NOT NULL;

-- ============================================================================
-- Section 18: corporate member search fix
-- ----------------------------------------------------------------------------
-- The first version matched the search term against the mobile number after
-- stripping non-digits. For a name such as 'Karim' that strip produces an
-- empty string, so the clause degraded to `mobile LIKE '%%'` and every
-- employee matched — the name filter silently did nothing. The digit clause
-- now only applies when the term actually contains digits.
-- ============================================================================

CREATE OR REPLACE FUNCTION app_security.corporate_members(p_search text DEFAULT NULL)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    WITH term AS (
        SELECT
            btrim(COALESCE(p_search, '')) AS raw,
            NULLIF(regexp_replace(COALESCE(p_search, ''), '\D', '', 'g'), '') AS digits
    )
    SELECT CASE WHEN app_security.corporate_client_id() IS NULL THEN '[]'::jsonb ELSE
        COALESCE((
        SELECT jsonb_agg(member ORDER BY member->>'full_name')
        FROM (
            SELECT jsonb_build_object(
                'id', e.id, 'full_name', e.full_name, 'mobile', e.mobile,
                'email', e.email, 'employee_code', e.employee_code,
                'requests_count', (SELECT count(*) FROM public.complaints x
                                   WHERE x.customer_id = e.id
                                     AND x.corporate_client_id = e.corporate_client_id),
                'requests', COALESCE((
                    SELECT jsonb_agg(jsonb_build_object(
                        'id', x.id, 'ticket_no', x.ticket_no, 'subject', x.subject,
                        'status', x.status::text, 'channel', x.channel::text,
                        'intake_source', x.intake_source,
                        'policy_number', x.policy_number, 'card_number', x.card_number,
                        'created_at', x.created_at, 'updated_at', x.updated_at)
                        ORDER BY x.created_at DESC)
                    FROM public.complaints x
                    WHERE x.customer_id = e.id
                      AND x.corporate_client_id = e.corporate_client_id), '[]'::jsonb)
            ) AS member
            FROM public.customers e, term t
            WHERE e.corporate_client_id = app_security.corporate_client_id()
              AND (
                    t.raw = ''
                    OR e.full_name ILIKE '%' || t.raw || '%'
                    OR e.employee_code ILIKE '%' || t.raw || '%'
                    OR (t.digits IS NOT NULL AND e.mobile LIKE '%' || t.digits || '%')
                  )
            LIMIT 500
        ) rows), '[]'::jsonb)
    END
$function$;

-- Section 19: guest tracking must accept REQ- tickets
--
-- `guest_ticket_status` carried its own copy of the ticket pattern and still
-- demanded a CMP- prefix, so every ticket minted after this migration failed
-- guest tracking with a 404 — the customer-visible "track without an account"
-- flow. The constraint in section 4 and this guard must agree.
CREATE OR REPLACE FUNCTION app_security.guest_ticket_status(p_ticket text, p_blind bytea)
RETURNS TABLE (public_ticket text, public_status text, public_company text,
    created timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'guest' OR octet_length(p_blind) <> 32
       OR p_ticket !~ '^(REQ|CMP)-[0-9]{4}-[0-9]{6,}$' THEN
        RETURN;
    END IF;
    SELECT c.id, k.ticket_no, c.status::text, co.name_en, c.created_at
        INTO v_id, public_ticket, public_status, public_company, created
        FROM public.complaint_keys k
        JOIN public.complaints c ON c.id = k.id AND c.created_at = k.created_at
        JOIN public.customers cu ON cu.id = c.customer_id
        JOIN public.insurance_companies co ON co.id = c.company_id
        WHERE k.ticket_no = p_ticket AND cu.national_id_blind_index = p_blind;
    IF FOUND THEN
        INSERT INTO public.audit_logs
            (action, resource_type, resource_id, complaint_id, complaint_created_at)
            VALUES ('guest_track', 'complaint', v_id, v_id, created);
        RETURN NEXT;
    END IF;
END;
$function$;
