-- Phase 2 is additive: existing keys, partitions and policies are preserved.
ALTER TABLE insurance_companies ADD COLUMN supported_types insurance_type[] NOT NULL
    DEFAULT ARRAY['health', 'general', 'both']::insurance_type[];
ALTER TABLE customers ADD COLUMN token_version integer NOT NULL DEFAULT 0;
ALTER TABLE customers ADD COLUMN national_id_last4 varchar(4);
ALTER TABLE customers ADD CONSTRAINT ck_customer_token_version CHECK (token_version >= 0);
ALTER TABLE customers ADD CONSTRAINT ck_customer_last4 CHECK (
    national_id_last4 IS NULL OR national_id_last4 ~ '^[0-9]{4}$');
ALTER TABLE staff_users ADD COLUMN two_factor_required boolean NOT NULL DEFAULT false;

CREATE TABLE security_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    staff_2fa_required boolean NOT NULL DEFAULT false,
    updated_by uuid REFERENCES staff_users(id),
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO security_settings (singleton) VALUES (true);

CREATE TABLE auth_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id uuid NOT NULL,
    subject_kind text NOT NULL CHECK (subject_kind IN ('staff', 'customer')),
    refresh_digest bytea NOT NULL UNIQUE CHECK (octet_length(refresh_digest) = 32),
    family_id uuid NOT NULL,
    rotated_from_id uuid REFERENCES auth_sessions(id),
    token_version integer NOT NULL CHECK (token_version >= 0),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_used_at timestamptz,
    ip_address inet,
    user_agent varchar(256),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_auth_sessions_subject ON auth_sessions (subject_kind, subject_id, created_at DESC);
CREATE INDEX ix_auth_sessions_family ON auth_sessions (family_id) WHERE revoked_at IS NULL;

CREATE TABLE staff_invitations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    staff_user_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    token_digest bytea NOT NULL UNIQUE CHECK (octet_length(token_digest) = 32),
    created_by uuid NOT NULL REFERENCES staff_users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz,
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_invitations_staff ON staff_invitations (staff_user_id, created_at DESC);

CREATE TABLE complaint_idempotency (
    customer_id uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    key_digest bytea NOT NULL CHECK (octet_length(key_digest) = 32),
    request_digest bytea NOT NULL CHECK (octet_length(request_digest) = 32),
    complaint_id uuid,
    complaint_created_at timestamptz,
    ticket_no text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (customer_id, key_digest),
    FOREIGN KEY (complaint_id, complaint_created_at, ticket_no)
        REFERENCES complaint_keys (id, created_at, ticket_no),
    CHECK ((complaint_id IS NULL AND complaint_created_at IS NULL AND ticket_no IS NULL)
        OR (complaint_id IS NOT NULL AND complaint_created_at IS NOT NULL AND ticket_no IS NOT NULL))
);
CREATE INDEX ix_complaint_idempotency_expiry ON complaint_idempotency (expires_at);

CREATE TABLE attachment_uploads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    uploaded_by_staff_id uuid REFERENCES staff_users(id),
    uploaded_by_customer_id uuid REFERENCES customers(id),
    visibility message_visibility NOT NULL DEFAULT 'public',
    storage_key text NOT NULL UNIQUE,
    original_filename varchar(255) NOT NULL,
    declared_mime varchar(128) NOT NULL,
    expected_checksum_sha256 text CHECK (expected_checksum_sha256 ~ '^[0-9a-f]{64}$'),
    max_size_bytes bigint NOT NULL CHECK (max_size_bytes BETWEEN 1 AND 10485760),
    status text NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'uploaded', 'rejected')),
    expires_at timestamptz NOT NULL,
    finalized_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE,
    CHECK ((uploaded_by_staff_id IS NULL) <> (uploaded_by_customer_id IS NULL)),
    CHECK (expires_at > created_at)
);
CREATE INDEX ix_attachment_uploads_complaint ON attachment_uploads
    (complaint_id, complaint_created_at, created_at DESC);
ALTER TABLE attachments ADD COLUMN upload_id uuid UNIQUE REFERENCES attachment_uploads(id);

CREATE TABLE notification_outbox (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    recipient_customer_id uuid NOT NULL REFERENCES customers(id),
    kind text NOT NULL CHECK (kind IN ('created', 'status_changed', 'reply', 'reopened')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'processing', 'sent', 'pending_provider', 'failed')),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 10),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE
);
CREATE INDEX ix_notification_outbox_pending ON notification_outbox (next_attempt_at, id)
    WHERE state IN ('queued', 'processing');
