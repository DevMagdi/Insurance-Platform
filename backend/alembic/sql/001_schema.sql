-- Immutable Phase 1 DDL. PostgreSQL 16+; Alembic applies statements transactionally.
-- Store UTC timestamps. All partitioned FKs include the immutable parent created_at.
CREATE SCHEMA app_security;

CREATE TYPE insurance_type AS ENUM ('health', 'general', 'both');
CREATE TYPE complaint_status AS ENUM ('new', 'assigned', 'in_review', 'resolved', 'reopened', 'transferred');
CREATE TYPE complaint_priority AS ENUM ('low', 'normal', 'high', 'urgent');
CREATE TYPE staff_status AS ENUM ('invited', 'active', 'disabled');
CREATE TYPE permission_effect AS ENUM ('allow', 'deny');
CREATE TYPE ownership_level AS ENUM ('own', 'team', 'all');
CREATE TYPE message_visibility AS ENUM ('public', 'internal');
CREATE TYPE communication_channel AS ENUM ('portal', 'email', 'whatsapp', 'phone', 'ai_voice', 'sms');
CREATE TYPE message_direction AS ENUM ('inbound', 'outbound');
CREATE TYPE attachment_scan_status AS ENUM ('pending', 'clean', 'infected', 'error');

CREATE TABLE insurance_companies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug text NOT NULL UNIQUE,
    name_en text NOT NULL,
    name_ar text NOT NULL,
    contact_emails text[] NOT NULL DEFAULT ARRAY[]::text[],
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_company_slug CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$')
);

CREATE TABLE customers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name text NOT NULL,
    national_id_ciphertext bytea NOT NULL,
    national_id_blind_index bytea NOT NULL UNIQUE,
    mobile text NOT NULL,
    whatsapp_available boolean NOT NULL DEFAULT false,
    email text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_customer_blind_index CHECK (octet_length(national_id_blind_index) = 32),
    CONSTRAINT ck_customer_mobile CHECK (mobile ~ '^\+201[0-9]{9}$'),
    CONSTRAINT ck_customer_full_name CHECK (length(btrim(full_name)) > 0)
);
CREATE INDEX ix_customers_mobile ON customers (mobile);

CREATE TABLE staff_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email text NOT NULL UNIQUE,
    full_name text NOT NULL,
    password_hash text,
    status staff_status NOT NULL DEFAULT 'invited',
    must_change_password boolean NOT NULL DEFAULT true,
    two_factor_enabled boolean NOT NULL DEFAULT false,
    totp_secret_ciphertext bytea,
    last_login_at timestamptz,
    token_version integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_staff_email_lower CHECK (email = lower(email)),
    CONSTRAINT ck_staff_token_version CHECK (token_version >= 0),
    CONSTRAINT ck_staff_two_factor_secret CHECK (NOT two_factor_enabled OR totp_secret_ciphertext IS NOT NULL)
);

CREATE TABLE permissions (
    code text PRIMARY KEY,
    resource text NOT NULL,
    action text NOT NULL,
    description text NOT NULL DEFAULT '',
    CONSTRAINT ck_permission_code CHECK (code = resource || '.' || action)
);
CREATE TABLE roles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    is_system boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE role_permissions (
    role_id uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_code text NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_code)
);
CREATE TABLE user_roles (
    user_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    role_id uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);
CREATE INDEX ix_user_roles_role ON user_roles (role_id, user_id);
CREATE TABLE user_permission_overrides (
    user_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    permission_code text NOT NULL REFERENCES permissions(code) ON DELETE CASCADE,
    effect permission_effect NOT NULL,
    PRIMARY KEY (user_id, permission_code)
);

CREATE TABLE teams (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    lead_staff_id uuid REFERENCES staff_users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE team_members (
    team_id uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX ix_team_members_user ON team_members (user_id, team_id);

-- At least one company row and exactly one ownership row are required to read complaints.
-- An empty insurance-type set means unrestricted; otherwise membership is mandatory.
CREATE TABLE user_company_scope (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    company_id uuid REFERENCES insurance_companies(id) ON DELETE CASCADE,
    all_companies boolean NOT NULL DEFAULT false,
    CONSTRAINT ck_company_scope_mode CHECK (
        (all_companies AND company_id IS NULL) OR (NOT all_companies AND company_id IS NOT NULL)
    ),
    UNIQUE (user_id, company_id)
);
CREATE UNIQUE INDEX ux_user_company_all ON user_company_scope (user_id) WHERE all_companies;
CREATE INDEX ix_user_company_company ON user_company_scope (company_id, user_id);
CREATE TABLE user_insurance_type_scope (
    user_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    insurance_type insurance_type NOT NULL,
    PRIMARY KEY (user_id, insurance_type)
);
CREATE TABLE user_ownership_level (
    user_id uuid PRIMARY KEY REFERENCES staff_users(id) ON DELETE CASCADE,
    level ownership_level NOT NULL
);
CREATE TABLE access_change_log (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_staff_id uuid NOT NULL REFERENCES staff_users(id),
    target_user_id uuid NOT NULL REFERENCES staff_users(id),
    change_type text NOT NULL,
    before_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_access_change_target ON access_change_log (target_user_id, created_at DESC);

CREATE TABLE policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id uuid NOT NULL REFERENCES customers(id),
    company_id uuid NOT NULL REFERENCES insurance_companies(id),
    insurance_type insurance_type NOT NULL,
    policy_number text NOT NULL,
    effective_from date,
    expires_at date,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, policy_number),
    UNIQUE (id, customer_id, company_id, insurance_type, policy_number),
    CONSTRAINT ck_policy_number CHECK (length(btrim(policy_number)) > 0)
);
CREATE INDEX ix_policies_customer ON policies (customer_id, company_id);

CREATE TABLE categories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES insurance_companies(id),
    parent_id uuid REFERENCES categories(id),
    name_en text NOT NULL,
    name_ar text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_categories_company ON categories (company_id, is_active);

-- The unpartitioned identity registry enforces globally unique ticket numbers and
-- locates a complaint's partition. A transaction creates the registry row first.
CREATE SEQUENCE complaint_ticket_sequence;
CREATE TABLE complaint_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL,
    ticket_no text NOT NULL UNIQUE,
    UNIQUE (id, created_at, ticket_no),
    CONSTRAINT ck_ticket_no CHECK (ticket_no ~ '^CMP-[0-9]{4}-[0-9]{6,}$'),
    CONSTRAINT ck_ticket_year CHECK (substring(ticket_no FROM 5 FOR 4) =
        to_char(created_at AT TIME ZONE 'UTC', 'YYYY'))
);
CREATE INDEX ix_complaint_keys_created ON complaint_keys (created_at DESC, id DESC);

CREATE TABLE complaints (
    id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    ticket_no text NOT NULL,
    customer_id uuid NOT NULL REFERENCES customers(id),
    company_id uuid NOT NULL REFERENCES insurance_companies(id),
    insurance_type insurance_type NOT NULL,
    policy_id uuid REFERENCES policies(id),
    policy_number text NOT NULL,
    subject text NOT NULL,
    description text NOT NULL,
    status complaint_status NOT NULL DEFAULT 'new',
    priority complaint_priority NOT NULL DEFAULT 'normal',
    category_id uuid REFERENCES categories(id),
    sentiment text,
    channel communication_channel NOT NULL DEFAULT 'portal',
    assigned_agent_id uuid REFERENCES staff_users(id),
    team_id uuid REFERENCES teams(id),
    sla_due_at timestamptz,
    resolved_at timestamptz,
    reopened_count integer NOT NULL DEFAULT 0,
    csat_score smallint,
    updated_at timestamptz NOT NULL DEFAULT now(),
    search_vector tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, coalesce(subject, '') || ' ' || coalesce(description, ''))
    ) STORED,
    PRIMARY KEY (id, created_at),
    UNIQUE (ticket_no, created_at),
    FOREIGN KEY (id, created_at, ticket_no)
        REFERENCES complaint_keys (id, created_at, ticket_no) ON DELETE RESTRICT,
    FOREIGN KEY (policy_id, customer_id, company_id, insurance_type, policy_number)
        REFERENCES policies (id, customer_id, company_id, insurance_type, policy_number),
    CONSTRAINT ck_complaint_subject CHECK (length(btrim(subject)) > 0),
    CONSTRAINT ck_complaint_policy_number CHECK (length(btrim(policy_number)) > 0),
    CONSTRAINT ck_complaint_reopened_count CHECK (reopened_count >= 0),
    CONSTRAINT ck_complaint_csat CHECK (csat_score BETWEEN 1 AND 5),
    CONSTRAINT ck_complaint_sentiment CHECK (sentiment IN ('positive', 'neutral', 'negative'))
) PARTITION BY RANGE (created_at);
-- On a partitioned table, parent indexes create matching local indexes for every month.
CREATE INDEX ix_complaints_keyset ON complaints (created_at DESC, id DESC);
CREATE INDEX ix_complaints_status_date ON complaints (status, created_at DESC, id DESC);
CREATE INDEX ix_complaints_company_status_date ON complaints (company_id, status, created_at DESC, id DESC);
CREATE INDEX ix_complaints_type_date ON complaints (insurance_type, created_at DESC, id DESC);
CREATE INDEX ix_complaints_agent_status_date ON complaints (assigned_agent_id, status, created_at DESC, id DESC);
CREATE INDEX ix_complaints_customer_date ON complaints (customer_id, created_at DESC, id DESC);
CREATE INDEX ix_complaints_team_date ON complaints (team_id, created_at DESC, id DESC);
CREATE INDEX ix_complaints_fts ON complaints USING gin (search_vector);

CREATE TABLE complaint_events (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    event_type text NOT NULL,
    from_status complaint_status,
    to_status complaint_status,
    actor_staff_id uuid REFERENCES staff_users(id),
    actor_customer_id uuid REFERENCES customers(id),
    visibility message_visibility NOT NULL DEFAULT 'public',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, occurred_at),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE
) PARTITION BY RANGE (occurred_at);
CREATE INDEX ix_events_timeline ON complaint_events
    (complaint_id, complaint_created_at, occurred_at DESC, id DESC);

CREATE TABLE complaint_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    body text NOT NULL,
    visibility message_visibility NOT NULL DEFAULT 'public',
    channel communication_channel NOT NULL DEFAULT 'portal',
    direction message_direction NOT NULL DEFAULT 'inbound',
    sender_staff_id uuid REFERENCES staff_users(id),
    sender_customer_id uuid REFERENCES customers(id),
    external_message_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    UNIQUE (id, complaint_id, complaint_created_at)
);
CREATE INDEX ix_messages_timeline ON complaint_messages
    (complaint_id, complaint_created_at, created_at DESC, id DESC);
CREATE UNIQUE INDEX ux_messages_external ON complaint_messages (channel, external_message_id)
    WHERE external_message_id IS NOT NULL;

CREATE TABLE attachments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    message_id uuid,
    uploaded_by_staff_id uuid REFERENCES staff_users(id),
    uploaded_by_customer_id uuid REFERENCES customers(id),
    visibility message_visibility NOT NULL DEFAULT 'public',
    storage_key text NOT NULL UNIQUE,
    original_filename text NOT NULL,
    mime_type text NOT NULL,
    size_bytes bigint NOT NULL,
    checksum_sha256 text NOT NULL,
    scan_status attachment_scan_status NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    FOREIGN KEY (message_id, complaint_id, complaint_created_at)
        REFERENCES complaint_messages (id, complaint_id, complaint_created_at) ON DELETE CASCADE,
    CONSTRAINT ck_attachment_size CHECK (size_bytes > 0),
    CONSTRAINT ck_attachment_checksum CHECK (checksum_sha256 ~ '^[0-9a-f]{64}$')
);
CREATE INDEX ix_attachments_complaint ON attachments (complaint_id, complaint_created_at, created_at DESC);

CREATE TABLE notifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_staff_id uuid REFERENCES staff_users(id) ON DELETE CASCADE,
    recipient_customer_id uuid REFERENCES customers(id) ON DELETE CASCADE,
    complaint_id uuid,
    complaint_created_at timestamptz,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    CONSTRAINT ck_notification_recipient CHECK ((recipient_staff_id IS NULL) <> (recipient_customer_id IS NULL)),
    CONSTRAINT ck_notification_complaint_pair CHECK ((complaint_id IS NULL) = (complaint_created_at IS NULL))
);
CREATE INDEX ix_notifications_staff ON notifications (recipient_staff_id, created_at DESC, id DESC);
CREATE INDEX ix_notifications_customer ON notifications (recipient_customer_id, created_at DESC, id DESC);

CREATE TABLE sla_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES insurance_companies(id) ON DELETE CASCADE,
    insurance_type insurance_type,
    priority complaint_priority,
    response_minutes integer NOT NULL,
    resolution_minutes integer NOT NULL,
    business_hours jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_sla_minutes CHECK (response_minutes > 0 AND resolution_minutes > 0)
);
CREATE INDEX ix_sla_match ON sla_rules (company_id, insurance_type, priority, is_active);

CREATE TABLE canned_replies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title text NOT NULL,
    body_en text NOT NULL,
    body_ar text NOT NULL,
    company_id uuid REFERENCES insurance_companies(id),
    created_by_staff_id uuid REFERENCES staff_users(id),
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE saved_filters (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_staff_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    name text NOT NULL,
    criteria jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_staff_id, name)
);

-- Provider secrets are never stored in cleartext JSON. secret_ref resolves in a vault.
CREATE TABLE integrations_config (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES insurance_companies(id),
    provider text NOT NULL,
    config_ciphertext bytea,
    secret_ref text,
    is_enabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_integrations_company_provider ON integrations_config
    (company_id, provider) NULLS NOT DISTINCT;
CREATE TABLE webhooks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES insurance_companies(id),
    url text NOT NULL,
    event_types text[] NOT NULL DEFAULT ARRAY[]::text[],
    signing_secret_ref text NOT NULL,
    is_active boolean NOT NULL DEFAULT true,
    last_delivery_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_webhook_https CHECK (url ~ '^https://')
);
CREATE TABLE api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_staff_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    key_prefix text NOT NULL UNIQUE,
    key_hash bytea NOT NULL UNIQUE,
    permission_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
    expires_at timestamptz,
    revoked_at timestamptz,
    last_used_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_api_key_hash CHECK (octet_length(key_hash) = 32)
);

CREATE TABLE call_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    direction message_direction NOT NULL,
    provider_call_id text,
    transcript_ciphertext bytea,
    recording_storage_key text,
    outcome text,
    consent_at timestamptz,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    CONSTRAINT ck_call_time CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX ix_call_logs_complaint ON call_logs (complaint_id, complaint_created_at, started_at DESC);

-- Queued human-reviewed AI outputs contain encrypted JSON, never free-form PII.
CREATE TABLE ai_insights (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    kind text NOT NULL,
    payload_ciphertext bytea NOT NULL,
    confidence numeric(5, 4),
    provider_model text,
    review_status text NOT NULL DEFAULT 'suggested',
    reviewed_by_staff_id uuid REFERENCES staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    CONSTRAINT ck_ai_kind CHECK (kind IN ('classification', 'sentiment', 'summary', 'reply')),
    CONSTRAINT ck_ai_confidence CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_ai_review CHECK (review_status IN ('suggested', 'accepted', 'rejected'))
);
CREATE INDEX ix_ai_insights_complaint ON ai_insights
    (complaint_id, complaint_created_at, created_at DESC);

-- A candidate is visible only when BOTH complaints are in scope. Order UUIDs
-- canonically to avoid mirrored pairs; no merge or disclosure is automatic.
CREATE TABLE duplicate_candidates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    first_complaint_id uuid NOT NULL,
    first_complaint_created_at timestamptz NOT NULL,
    second_complaint_id uuid NOT NULL,
    second_complaint_created_at timestamptz NOT NULL,
    confidence numeric(5, 4) NOT NULL,
    decision text NOT NULL DEFAULT 'pending',
    reviewed_by_staff_id uuid REFERENCES staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (first_complaint_id, first_complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    FOREIGN KEY (second_complaint_id, second_complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    UNIQUE (first_complaint_id, first_complaint_created_at,
            second_complaint_id, second_complaint_created_at),
    CONSTRAINT ck_duplicate_order CHECK (first_complaint_id < second_complaint_id),
    CONSTRAINT ck_duplicate_confidence CHECK (confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_duplicate_decision CHECK (decision IN ('pending', 'confirmed', 'dismissed'))
);
CREATE INDEX ix_duplicate_second ON duplicate_candidates
    (second_complaint_id, second_complaint_created_at);

CREATE TABLE csat_responses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    customer_id uuid NOT NULL REFERENCES customers(id),
    score smallint NOT NULL,
    comment text,
    submitted_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    CONSTRAINT ck_csat_score CHECK (score BETWEEN 1 AND 5)
);
CREATE INDEX ix_csat_complaint ON csat_responses
    (complaint_id, complaint_created_at, submitted_at DESC);

CREATE TABLE assignment_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid REFERENCES insurance_companies(id),
    insurance_type insurance_type,
    category_id uuid REFERENCES categories(id),
    priority complaint_priority,
    target_team_id uuid REFERENCES teams(id),
    target_agent_id uuid REFERENCES staff_users(id),
    sort_order integer NOT NULL DEFAULT 100,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_assignment_target CHECK (target_team_id IS NOT NULL OR target_agent_id IS NOT NULL)
);
CREATE INDEX ix_assignment_match ON assignment_rules
    (company_id, insurance_type, priority, sort_order) WHERE is_active;

-- Senders/recipients and export downloads MUST be reauthorized at execution
-- time, not using a long-lived snapshot of their original company scope.
CREATE TABLE report_schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_staff_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    name text NOT NULL,
    filters jsonb NOT NULL DEFAULT '{}'::jsonb,
    cron_expression text NOT NULL,
    time_zone text NOT NULL DEFAULT 'Africa/Cairo',
    recipient_emails text[] NOT NULL DEFAULT ARRAY[]::text[],
    next_run_at timestamptz,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner_staff_id, name)
);
CREATE INDEX ix_report_schedules_due ON report_schedules (next_run_at) WHERE is_active;
CREATE TABLE export_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_staff_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    kind text NOT NULL,
    filters jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'queued',
    private_storage_key text,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT ck_export_kind CHECK (kind IN ('complaints', 'analytics')),
    CONSTRAINT ck_export_status CHECK (status IN ('queued', 'running', 'completed', 'failed', 'expired'))
);
CREATE INDEX ix_export_jobs_owner ON export_jobs
    (owner_staff_id, created_at DESC, id DESC);

CREATE TABLE audit_logs (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    actor_staff_id uuid REFERENCES staff_users(id),
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    complaint_id uuid,
    complaint_created_at timestamptz,
    before_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_state jsonb NOT NULL DEFAULT '{}'::jsonb,
    request_id uuid,
    ip_address inet,
    PRIMARY KEY (id, created_at),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE SET NULL (complaint_id, complaint_created_at),
    CONSTRAINT ck_audit_complaint_pair CHECK ((complaint_id IS NULL) = (complaint_created_at IS NULL))
) PARTITION BY RANGE (created_at);
CREATE INDEX ix_audit_keyset ON audit_logs (created_at DESC, id DESC);
CREATE INDEX ix_audit_complaint ON audit_logs (complaint_id, complaint_created_at, created_at DESC);
CREATE INDEX ix_audit_actor ON audit_logs (actor_staff_id, created_at DESC);

-- Covers the previous 24 months and next 24 months. The default partition is a
-- safety net, not the steady state: run the maintenance task before the horizon.
DO $partitions$
DECLARE
    month_start date := (date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - interval '24 months')::date;
    last_month date := (date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC') + interval '24 months')::date;
    parent_name text;
    start_bound text;
    end_bound text;
BEGIN
    WHILE month_start <= last_month LOOP
        start_bound := to_char(month_start, 'YYYY-MM-DD') || ' 00:00:00+00';
        end_bound := to_char(month_start + interval '1 month', 'YYYY-MM-DD') || ' 00:00:00+00';
        FOREACH parent_name IN ARRAY ARRAY['complaints', 'complaint_events', 'audit_logs'] LOOP
            EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                parent_name || '_' || to_char(month_start, 'YYYYMM'), parent_name, start_bound, end_bound);
        END LOOP;
        month_start := (month_start + interval '1 month')::date;
    END LOOP;
END;
$partitions$;
CREATE TABLE complaints_default PARTITION OF complaints DEFAULT;
CREATE TABLE complaint_events_default PARTITION OF complaint_events DEFAULT;
CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT;
