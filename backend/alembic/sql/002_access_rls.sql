-- RLS is defence in depth: only the API sets these transaction-local GUCs after
-- authenticating a principal. app_runtime is NEVER a table owner or BYPASSRLS role.
-- SECURITY DEFINER functions are owned by the migration role (postgres in dev).
-- A caller cannot pass arbitrary actor IDs to the permission/scope functions.
CREATE FUNCTION app_security.actor_id() RETURNS uuid
LANGUAGE sql STABLE AS $function$
    SELECT CASE
        WHEN current_setting('app.user_id', true) ~*
            '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN current_setting('app.user_id', true)::uuid
        ELSE NULL::uuid
    END
$function$;

CREATE FUNCTION app_security.actor_scope() RETURNS text
LANGUAGE sql STABLE AS $function$
    SELECT current_setting('app.scope', true)
$function$;

CREATE FUNCTION app_security.is_active_staff() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT app_security.actor_scope() = 'staff' AND EXISTS (
        SELECT 1 FROM public.staff_users
        WHERE id = app_security.actor_id() AND status = 'active'
    )
$function$;

-- Deny overrides take priority, then allow overrides, then the union of role grants.
CREATE FUNCTION app_security.has_permission(p_code text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE(app_security.is_active_staff(), false) AND COALESCE(
        (SELECT CASE WHEN effect = 'deny' THEN false ELSE true END
         FROM public.user_permission_overrides
         WHERE user_id = app_security.actor_id() AND permission_code = p_code),
        EXISTS (
            SELECT 1 FROM public.user_roles ur
            JOIN public.role_permissions rp ON rp.role_id = ur.role_id
            WHERE ur.user_id = app_security.actor_id() AND rp.permission_code = p_code
        )
    )
$function$;

-- Company scope is mandatory; insurance type is optional (empty = unrestricted);
-- ownership is mandatory, and a team match requires real team membership.
CREATE FUNCTION app_security.staff_scope_matches(
    p_company_id uuid, p_insurance_type insurance_type, p_agent_id uuid, p_team_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE(app_security.is_active_staff(), false)
        AND EXISTS (
            SELECT 1 FROM public.user_company_scope s
            WHERE s.user_id = app_security.actor_id()
              AND (s.all_companies OR s.company_id = p_company_id)
        )
        AND (
            NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                        WHERE t.user_id = app_security.actor_id())
            OR EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                       WHERE t.user_id = app_security.actor_id()
                         AND t.insurance_type = p_insurance_type)
        )
        AND EXISTS (
            SELECT 1 FROM public.user_ownership_level o
            WHERE o.user_id = app_security.actor_id() AND (
                o.level = 'all'
                OR (o.level = 'own' AND p_agent_id = app_security.actor_id())
                OR (o.level = 'team' AND EXISTS (
                    SELECT 1 FROM public.team_members tm
                    WHERE tm.team_id = p_team_id AND tm.user_id = app_security.actor_id()
                ))
            )
        )
$function$;

CREATE FUNCTION app_security.has_global_scope() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE(app_security.is_active_staff(), false)
      AND EXISTS (SELECT 1 FROM public.user_company_scope
                  WHERE user_id = app_security.actor_id() AND all_companies)
      AND NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope
                      WHERE user_id = app_security.actor_id())
      AND EXISTS (SELECT 1 FROM public.user_ownership_level
                  WHERE user_id = app_security.actor_id() AND level = 'all')
$function$;

-- Company-scoped metadata (categories/SLA/templates) must not expose another
-- insurer's operational settings. Public insurer names use a separate safe grant.
CREATE FUNCTION app_security.has_company_scope(p_company_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE(app_security.is_active_staff(), false) AND EXISTS (
        SELECT 1 FROM public.user_company_scope s
        WHERE s.user_id = app_security.actor_id()
          AND (p_company_id IS NULL OR s.all_companies OR s.company_id = p_company_id)
    )
$function$;

CREATE FUNCTION app_security.can_read_complaint(
    p_company_id uuid, p_insurance_type insurance_type, p_agent_id uuid,
    p_team_id uuid, p_customer_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE app_security.actor_scope()
        WHEN 'customer' THEN COALESCE(app_security.actor_id() = p_customer_id, false)
        WHEN 'staff' THEN app_security.has_permission('complaints.view')
            AND app_security.staff_scope_matches(
                p_company_id, p_insurance_type, p_agent_id, p_team_id
            )
        ELSE false
    END
$function$;

-- The trusted definer has BYPASSRLS (superuser in dev); an ordinary table owner
-- CANNOT bypass FORCE RLS. Child policies must not recurse into the parent's
-- policy. The WHERE clause resolves one immutable composite key.
CREATE FUNCTION app_security.can_read_complaint_id(
    p_complaint_id uuid, p_created_at timestamptz
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE((
        SELECT app_security.can_read_complaint(
            c.company_id, c.insurance_type, c.assigned_agent_id, c.team_id, c.customer_id
        )
        FROM public.complaints c
        WHERE c.id = p_complaint_id AND c.created_at = p_created_at
    ), false)
$function$;

CREATE FUNCTION app_security.can_read_customer(p_customer_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE app_security.actor_scope()
        WHEN 'customer' THEN COALESCE(app_security.actor_id() = p_customer_id, false)
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

-- No blanket SELECT privilege on ciphertext or its blind index. Explicit decryption
-- still requires the server-held key; this function checks the stronger permission.
CREATE FUNCTION app_security.read_national_id_ciphertext(p_customer_id uuid) RETURNS bytea
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE result bytea;
BEGIN
    IF NOT app_security.has_permission('complaints.view_full_national_id')
       OR NOT app_security.can_read_customer(p_customer_id) THEN
        RETURN NULL;
    END IF;
    SELECT national_id_ciphertext INTO result
    FROM public.customers WHERE id = p_customer_id;
    RETURN result;
END;
$function$;

CREATE FUNCTION app_security.issue_ticket_number(p_created_at timestamptz) RETURNS text
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT 'CMP-' || to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY') || '-'
        || lpad(seq.n::text, GREATEST(6, length(seq.n::text)), '0')
    FROM (SELECT nextval('public.complaint_ticket_sequence') AS n) AS seq
$function$;

ALTER TABLE complaints ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaints FORCE ROW LEVEL SECURITY;
CREATE POLICY complaints_read ON complaints FOR SELECT TO app_runtime
    USING (app_security.can_read_complaint(
        company_id, insurance_type, assigned_agent_id, team_id, customer_id));
CREATE POLICY complaints_insert ON complaints FOR INSERT TO app_runtime
    WITH CHECK (
        (app_security.actor_scope() = 'customer' AND customer_id = app_security.actor_id())
        OR (app_security.has_permission('complaints.create')
            AND app_security.staff_scope_matches(
                company_id, insurance_type, assigned_agent_id, team_id))
    );
-- UPDATE privilege is column-limited below. Service code additionally checks
-- resource.action for each specific command; 'edit' is the DB write floor.
CREATE POLICY complaints_update ON complaints FOR UPDATE TO app_runtime
    USING (app_security.has_permission('complaints.edit')
           AND app_security.can_read_complaint(
               company_id, insurance_type, assigned_agent_id, team_id, customer_id))
    WITH CHECK (app_security.has_permission('complaints.edit')
           AND app_security.can_read_complaint(
               company_id, insurance_type, assigned_agent_id, team_id, customer_id));

ALTER TABLE complaint_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaint_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY complaint_keys_read ON complaint_keys FOR SELECT TO app_runtime
    USING (app_security.can_read_complaint_id(id, created_at));
CREATE POLICY complaint_keys_insert ON complaint_keys FOR INSERT TO app_runtime
    WITH CHECK (app_security.actor_scope() = 'customer'
                OR app_security.has_permission('complaints.create'));

ALTER TABLE complaint_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaint_events FORCE ROW LEVEL SECURITY;
CREATE POLICY complaint_events_read ON complaint_events FOR SELECT TO app_runtime
    USING (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
           AND (app_security.actor_scope() = 'staff' OR visibility = 'public'));
CREATE POLICY complaint_events_insert ON complaint_events FOR INSERT TO app_runtime
    WITH CHECK (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
        AND ((app_security.actor_scope() = 'staff'
              AND app_security.has_permission('complaints.change_status'))
          OR (app_security.actor_scope() = 'customer' AND visibility = 'public'
              AND event_type = 'reopened' AND actor_customer_id = app_security.actor_id())));

ALTER TABLE complaint_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE complaint_messages FORCE ROW LEVEL SECURITY;
CREATE POLICY complaint_messages_read ON complaint_messages FOR SELECT TO app_runtime
    USING (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
           AND (app_security.actor_scope() = 'staff' OR visibility = 'public'));
CREATE POLICY complaint_messages_insert ON complaint_messages FOR INSERT TO app_runtime
    WITH CHECK (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
       AND ((app_security.actor_scope() = 'staff' AND sender_staff_id = app_security.actor_id()
             AND ((visibility = 'public' AND app_security.has_permission('complaints.reply_public'))
               OR (visibility = 'internal' AND app_security.has_permission('complaints.add_internal_note'))))
         OR (app_security.actor_scope() = 'customer' AND visibility = 'public'
             AND direction = 'inbound' AND sender_customer_id = app_security.actor_id())));

ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachments FORCE ROW LEVEL SECURITY;
CREATE POLICY attachments_read ON attachments FOR SELECT TO app_runtime
    USING (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
       AND ((app_security.actor_scope() = 'staff'
             AND app_security.has_permission('complaints.view_attachments'))
         OR (app_security.actor_scope() = 'customer' AND visibility = 'public'
             AND scan_status = 'clean' AND (message_id IS NULL OR EXISTS (
                 SELECT 1 FROM public.complaint_messages m
                 WHERE m.id = message_id AND m.visibility = 'public'
             )))));
CREATE POLICY attachments_insert ON attachments FOR INSERT TO app_runtime
    WITH CHECK (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
       AND scan_status = 'pending'
       AND ((app_security.actor_scope() = 'staff'
             AND uploaded_by_staff_id = app_security.actor_id()
             AND app_security.has_permission('complaints.view_attachments'))
         OR (app_security.actor_scope() = 'customer' AND visibility = 'public'
             AND uploaded_by_customer_id = app_security.actor_id())));

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications FORCE ROW LEVEL SECURITY;
CREATE POLICY notifications_read ON notifications FOR SELECT TO app_runtime
    USING ((
        (app_security.is_active_staff() AND recipient_staff_id = app_security.actor_id())
        OR (app_security.actor_scope() = 'customer'
            AND recipient_customer_id = app_security.actor_id())
    ) AND (complaint_id IS NULL
        OR app_security.can_read_complaint_id(complaint_id, complaint_created_at)));

ALTER TABLE call_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY call_logs_read ON call_logs FOR SELECT TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND app_security.has_permission('integrations.ai_call')
        AND app_security.can_read_complaint_id(complaint_id, complaint_created_at));

ALTER TABLE ai_insights ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_insights FORCE ROW LEVEL SECURITY;
CREATE POLICY ai_insights_read ON ai_insights FOR SELECT TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND app_security.can_read_complaint_id(complaint_id, complaint_created_at));
ALTER TABLE duplicate_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE duplicate_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY duplicate_candidates_read ON duplicate_candidates FOR SELECT TO app_runtime
    USING (app_security.actor_scope() = 'staff'
        AND app_security.can_read_complaint_id(
            first_complaint_id, first_complaint_created_at)
        AND app_security.can_read_complaint_id(
            second_complaint_id, second_complaint_created_at));
ALTER TABLE csat_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE csat_responses FORCE ROW LEVEL SECURITY;
CREATE POLICY csat_responses_read ON csat_responses FOR SELECT TO app_runtime
    USING (app_security.can_read_complaint_id(complaint_id, complaint_created_at)
        AND ((app_security.actor_scope() = 'customer'
                AND customer_id = app_security.actor_id())
          OR (app_security.actor_scope() = 'staff'
              AND app_security.has_permission('analytics.view'))));

ALTER TABLE report_schedules ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_schedules FORCE ROW LEVEL SECURITY;
CREATE POLICY report_schedules_read ON report_schedules FOR SELECT TO app_runtime
    USING (app_security.is_active_staff() AND owner_staff_id = app_security.actor_id()
        AND app_security.has_permission('reports.manage'));
ALTER TABLE export_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY export_jobs_read ON export_jobs FOR SELECT TO app_runtime
    USING (app_security.is_active_staff() AND owner_staff_id = app_security.actor_id()
        AND ((kind = 'complaints' AND app_security.has_permission('complaints.export'))
          OR (kind = 'analytics' AND app_security.has_permission('analytics.export'))));
ALTER TABLE assignment_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignment_rules FORCE ROW LEVEL SECURITY;
CREATE POLICY assignment_rules_read ON assignment_rules FOR SELECT TO app_runtime
    USING (app_security.has_company_scope(company_id)
        AND app_security.has_permission('complaints.assign'));

ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_logs_read ON audit_logs FOR SELECT TO app_runtime
    USING (app_security.has_permission('audit.view')
       AND ((complaint_id IS NOT NULL
             AND app_security.can_read_complaint_id(complaint_id, complaint_created_at))
         OR (complaint_id IS NULL AND app_security.has_global_scope())));
CREATE POLICY audit_logs_insert ON audit_logs FOR INSERT TO app_runtime
    WITH CHECK (app_security.is_active_staff()
        AND actor_staff_id = app_security.actor_id()
        AND (complaint_id IS NULL
             OR app_security.can_read_complaint_id(complaint_id, complaint_created_at)));

ALTER TABLE access_change_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE access_change_log FORCE ROW LEVEL SECURITY;
CREATE POLICY access_change_log_read ON access_change_log FOR SELECT TO app_runtime
    USING (app_security.has_permission('audit.view')
        AND app_security.has_permission('users.manage_access')
        AND app_security.has_global_scope());

ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE customers FORCE ROW LEVEL SECURITY;
CREATE POLICY customers_read ON customers FOR SELECT TO app_runtime
    USING (app_security.can_read_customer(id));

ALTER TABLE policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE policies FORCE ROW LEVEL SECURITY;
CREATE POLICY policies_read ON policies FOR SELECT TO app_runtime
    USING (
        (app_security.actor_scope() = 'customer' AND customer_id = app_security.actor_id())
        OR (app_security.has_permission('complaints.view') AND EXISTS (
            SELECT 1 FROM public.complaints c
            WHERE c.policy_id = policies.id AND c.company_id = policies.company_id
              AND app_security.can_read_complaint(
                  c.company_id, c.insurance_type, c.assigned_agent_id, c.team_id, c.customer_id)
        ))
    );
CREATE INDEX ix_complaints_policy ON complaints (policy_id, created_at DESC)
    WHERE policy_id IS NOT NULL;

ALTER TABLE categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE categories FORCE ROW LEVEL SECURITY;
CREATE POLICY categories_read ON categories FOR SELECT TO app_runtime
    USING (app_security.has_company_scope(company_id)
        AND (app_security.has_permission('complaints.view')
             OR app_security.has_permission('categories.manage')));
ALTER TABLE sla_rules ENABLE ROW LEVEL SECURITY;
ALTER TABLE sla_rules FORCE ROW LEVEL SECURITY;
CREATE POLICY sla_rules_read ON sla_rules FOR SELECT TO app_runtime
    USING (app_security.has_company_scope(company_id)
        AND (app_security.has_permission('complaints.view')
             OR app_security.has_permission('sla.manage')));
ALTER TABLE canned_replies ENABLE ROW LEVEL SECURITY;
ALTER TABLE canned_replies FORCE ROW LEVEL SECURITY;
CREATE POLICY canned_replies_read ON canned_replies FOR SELECT TO app_runtime
    USING (app_security.has_company_scope(company_id)
        AND (app_security.has_permission('complaints.reply_public')
             OR app_security.has_permission('templates.manage')));

-- No direct access to permission/scope internals or provider secrets. Functions
-- execute as the migration owner, use a fixed search_path, and cannot be replaced
-- by app_runtime (which lacks CREATE on public/app_security).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA app_security FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT USAGE ON SCHEMA public, app_security TO app_runtime;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app_security TO app_runtime;
GRANT SELECT (id, slug, name_en, name_ar, is_active)
    ON insurance_companies TO app_runtime;
GRANT SELECT ON categories, sla_rules, canned_replies TO app_runtime;
GRANT SELECT (id, full_name, mobile, whatsapp_available, email, created_at, updated_at)
    ON customers TO app_runtime;
GRANT SELECT ON policies, complaint_keys, complaints, complaint_events,
    complaint_messages, attachments, notifications, call_logs, audit_logs,
    ai_insights, duplicate_candidates, csat_responses, report_schedules,
    assignment_rules, access_change_log TO app_runtime;
GRANT SELECT (id, owner_staff_id, kind, filters, status, expires_at, created_at, completed_at)
    ON export_jobs TO app_runtime; -- Storage key needs a separately audited download path.
GRANT INSERT (id, created_at, ticket_no) ON complaint_keys TO app_runtime;
GRANT INSERT (id, created_at, ticket_no, customer_id, company_id, insurance_type,
    policy_id, policy_number, subject, description, status, priority, category_id,
    sentiment, channel, assigned_agent_id, team_id, sla_due_at)
    ON complaints TO app_runtime;
GRANT UPDATE (subject, description, priority, category_id, status, sentiment,
    assigned_agent_id, team_id, sla_due_at, resolved_at, reopened_count, csat_score, updated_at)
    ON complaints TO app_runtime;
GRANT INSERT ON complaint_events, complaint_messages, attachments, audit_logs TO app_runtime;
