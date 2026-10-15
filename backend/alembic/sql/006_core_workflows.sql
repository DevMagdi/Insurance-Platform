-- Scoped access changes are atomic, re-check grants in PostgreSQL and revoke
-- sessions. A caller cannot edit their own grants, promote beyond effective
-- permissions, or remove the last active administrator.
CREATE FUNCTION app_security.replace_user_access(p_target uuid, p_spec jsonb) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_before jsonb; v_after jsonb; v_was_admin boolean; v_will_admin boolean;
v_all boolean; v_ownership text; v_new_version integer;
BEGIN
    IF NOT app_security.has_permission('users.manage_access')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id()
       OR NOT EXISTS (SELECT 1 FROM public.staff_users WHERE id = p_target)
       OR jsonb_typeof(p_spec) <> 'object' THEN
        RAISE EXCEPTION 'access change forbidden' USING ERRCODE = '42501';
    END IF;
    v_before := app_security.access_snapshot(p_target);
    v_all := COALESCE((p_spec->>'all_companies')::boolean, false);
    v_ownership := p_spec->>'ownership';
    IF v_ownership NOT IN ('own','team','all')
       OR jsonb_typeof(p_spec->'role_ids') <> 'array'
       OR jsonb_typeof(p_spec->'allow') <> 'array'
       OR jsonb_typeof(p_spec->'deny') <> 'array'
       OR jsonb_typeof(p_spec->'company_ids') <> 'array'
       OR jsonb_typeof(p_spec->'insurance_types') <> 'array'
       OR jsonb_typeof(p_spec->'team_ids') <> 'array'
       OR (NOT v_all AND jsonb_array_length(p_spec->'company_ids') = 0)
       OR (v_all AND jsonb_array_length(p_spec->'company_ids') > 0) THEN
        RAISE EXCEPTION 'invalid access specification' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'allow') code
            WHERE NOT app_security.has_permission(code))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'role_ids') rid
            JOIN public.role_permissions rp ON rp.role_id = rid::uuid
            WHERE NOT app_security.has_permission(rp.permission_code))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'role_ids') rid
            WHERE NOT EXISTS (SELECT 1 FROM public.roles r WHERE r.id = rid::uuid))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'allow') code
            WHERE NOT EXISTS (SELECT 1 FROM public.permissions p WHERE p.code = code))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'deny') code
            WHERE NOT EXISTS (SELECT 1 FROM public.permissions p WHERE p.code = code))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'allow') code
            WHERE code IN (SELECT jsonb_array_elements_text(p_spec->'deny')))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'insurance_types') kind
            WHERE kind NOT IN ('health','general','both'))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'company_ids') cid
            WHERE NOT EXISTS (SELECT 1 FROM public.insurance_companies co
                WHERE co.id = cid::uuid AND co.is_active))
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'team_ids') tid
            WHERE NOT EXISTS (SELECT 1 FROM public.teams t WHERE t.id = tid::uuid)) THEN
        RAISE EXCEPTION 'cannot grant permissions or scope not held' USING ERRCODE = '42501';
    END IF;
    -- Global managers have all company/type/ownership scope, but permission
    -- overrides still restrict what their roles may delegate.
    PERFORM pg_advisory_xact_lock(hashtext('insurance-platform:last-administrator'));
    SELECT EXISTS (SELECT 1 FROM public.user_roles ur JOIN public.roles r ON r.id = ur.role_id
        WHERE ur.user_id = p_target AND r.name = 'administrator') INTO v_was_admin;
    SELECT EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_spec->'role_ids') rid
        JOIN public.roles r ON r.id = rid::uuid WHERE r.name = 'administrator') INTO v_will_admin;
    IF v_was_admin AND NOT v_will_admin
       AND (SELECT status = 'active' FROM public.staff_users WHERE id = p_target)
       AND (SELECT count(DISTINCT ur.user_id) FROM public.user_roles ur
            JOIN public.roles r ON r.id = ur.role_id
            JOIN public.staff_users u ON u.id = ur.user_id
            WHERE r.name = 'administrator' AND u.status = 'active') <= 1 THEN
        RAISE EXCEPTION 'cannot remove last administrator' USING ERRCODE = '23514';
    END IF;
    DELETE FROM public.user_roles WHERE user_id = p_target;
    INSERT INTO public.user_roles (user_id, role_id)
        SELECT p_target, rid::uuid FROM
            (SELECT DISTINCT jsonb_array_elements_text(p_spec->'role_ids') rid) x;
    DELETE FROM public.user_permission_overrides WHERE user_id = p_target;
    INSERT INTO public.user_permission_overrides (user_id, permission_code, effect)
        SELECT p_target, code, 'allow'::permission_effect FROM
            (SELECT DISTINCT jsonb_array_elements_text(p_spec->'allow') code) x;
    INSERT INTO public.user_permission_overrides (user_id, permission_code, effect)
        SELECT p_target, code, 'deny'::permission_effect FROM
            (SELECT DISTINCT jsonb_array_elements_text(p_spec->'deny') code) x;
    DELETE FROM public.user_company_scope WHERE user_id = p_target;
    IF v_all THEN
        INSERT INTO public.user_company_scope (user_id, all_companies)
            VALUES (p_target, true);
    ELSE
        INSERT INTO public.user_company_scope (user_id, company_id)
            SELECT p_target, cid::uuid FROM
                (SELECT DISTINCT jsonb_array_elements_text(p_spec->'company_ids') cid) x;
    END IF;
    DELETE FROM public.user_insurance_type_scope WHERE user_id = p_target;
    INSERT INTO public.user_insurance_type_scope (user_id, insurance_type)
        SELECT p_target, kind::insurance_type FROM
            (SELECT DISTINCT jsonb_array_elements_text(p_spec->'insurance_types') kind) x;
    INSERT INTO public.user_ownership_level (user_id, level)
        VALUES (p_target, v_ownership::ownership_level)
        ON CONFLICT (user_id) DO UPDATE SET level = EXCLUDED.level;
    DELETE FROM public.team_members WHERE user_id = p_target;
    INSERT INTO public.team_members (user_id, team_id)
        SELECT p_target, tid::uuid FROM
            (SELECT DISTINCT jsonb_array_elements_text(p_spec->'team_ids') tid) x;
    UPDATE public.staff_users SET token_version = token_version + 1,
        updated_at = now() WHERE id = p_target RETURNING token_version INTO v_new_version;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    v_after := app_security.access_snapshot(p_target);
    INSERT INTO public.access_change_log
        (actor_staff_id, target_user_id, change_type, before_state, after_state)
        VALUES (app_security.actor_id(), p_target, 'access_replaced',
            v_before, v_after);
    RETURN v_new_version;
END;
$function$;

CREATE FUNCTION app_security.deactivate_staff(p_target uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_admin boolean;
BEGIN
    IF NOT app_security.has_permission('users.deactivate')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id() THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('insurance-platform:last-administrator'));
    SELECT EXISTS (SELECT 1 FROM public.user_roles ur JOIN public.roles r ON r.id = ur.role_id
        WHERE ur.user_id = p_target AND r.name = 'administrator') INTO v_admin;
    IF v_admin AND (SELECT count(DISTINCT ur.user_id) FROM public.user_roles ur
        JOIN public.roles r ON r.id = ur.role_id
        JOIN public.staff_users u ON u.id = ur.user_id
        WHERE r.name = 'administrator' AND u.status = 'active') <= 1 THEN
        RAISE EXCEPTION 'cannot disable last administrator' USING ERRCODE = '23514';
    END IF;
    UPDATE public.staff_users SET status = 'disabled', token_version = token_version + 1,
        updated_at = now() WHERE id = p_target AND status <> 'disabled';
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    INSERT INTO public.access_change_log
        (actor_staff_id, target_user_id, change_type, after_state)
        VALUES (app_security.actor_id(), p_target, 'deactivated',
            jsonb_build_object('status', 'disabled'));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.require_staff_totp(p_target uuid, p_required boolean)
RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_version integer;
BEGIN
    IF NOT app_security.has_permission('users.manage_access')
       OR NOT app_security.has_global_scope() OR p_target = app_security.actor_id() THEN
        RAISE EXCEPTION '2FA policy change forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.staff_users SET two_factor_required = p_required,
        token_version = token_version + 1, updated_at = now()
        WHERE id = p_target RETURNING token_version INTO v_version;
    IF v_version IS NULL THEN RETURN NULL; END IF;
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND subject_id = p_target AND revoked_at IS NULL;
    INSERT INTO public.access_change_log
        (actor_staff_id, target_user_id, change_type, after_state)
        VALUES (app_security.actor_id(), p_target, 'totp_policy',
            jsonb_build_object('two_factor_required', p_required));
    RETURN v_version;
END;
$function$;

CREATE FUNCTION app_security.require_global_totp(p_required boolean) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.has_permission('settings.manage') OR
       NOT app_security.has_global_scope() THEN
        RAISE EXCEPTION 'global 2FA policy change forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.security_settings SET staff_2fa_required = p_required,
        updated_at = now(), updated_by = app_security.actor_id() WHERE singleton;
    UPDATE public.staff_users SET token_version = token_version + 1, updated_at = now()
        WHERE status = 'active';
    UPDATE public.auth_sessions SET revoked_at = now()
        WHERE subject_kind = 'staff' AND revoked_at IS NULL;
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type, after_state)
        VALUES (app_security.actor_id(), 'global_totp_policy', 'settings',
            jsonb_build_object('required', p_required));
END;
$function$;

-- Creation is the only additional customer event allowed through direct INSERT.
CREATE POLICY complaint_events_customer_created ON complaint_events FOR INSERT TO app_runtime
    WITH CHECK (app_security.actor_scope() = 'customer'
       AND actor_customer_id = app_security.actor_id()
       AND app_security.can_read_complaint_id(complaint_id, complaint_created_at)
       AND visibility = 'public' AND event_type = 'created'
       AND from_status IS NULL AND to_status = 'new');

-- Guest tracking never reveals a name, mobile, ID, description, or message. A
-- blind-index + exact ticket match is necessary and every hit is audited.
CREATE FUNCTION app_security.guest_ticket_status(p_ticket text, p_blind bytea)
RETURNS TABLE (public_ticket text, public_status text, public_company text,
    created timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid;
BEGIN
    IF app_security.actor_scope() <> 'guest' OR octet_length(p_blind) <> 32
       OR p_ticket !~ '^CMP-[0-9]{4}-[0-9]{6,}$' THEN
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

CREATE FUNCTION app_security.audit_customer_action(
    p_id uuid, p_created timestamptz, p_action text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'customer'
       OR NOT app_security.can_read_complaint_id(p_id, p_created)
       OR p_action NOT IN ('complaint_created', 'reply', 'upload', 'download', 'csat') THEN
        RAISE EXCEPTION 'audit forbidden' USING ERRCODE = '42501';
    END IF;
    INSERT INTO public.audit_logs (action, resource_type, resource_id,
        complaint_id, complaint_created_at)
        VALUES (p_action, 'complaint', p_id, p_id, p_created);
END;
$function$;

CREATE FUNCTION app_security.queue_customer_notice(
    p_id uuid, p_created timestamptz, p_kind text) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_customer uuid; v_ticket text; v_status text; v_id uuid;
BEGIN
    IF NOT app_security.can_read_complaint_id(p_id, p_created)
       OR p_kind NOT IN ('created', 'status_changed', 'reply', 'reopened')
       OR (p_kind = 'created' AND app_security.actor_scope() <> 'customer')
       OR (p_kind = 'status_changed' AND
           NOT app_security.has_permission('complaints.change_status'))
       OR (p_kind = 'reply' AND app_security.actor_scope() = 'staff' AND
           NOT app_security.has_permission('complaints.reply_public')) THEN
        RAISE EXCEPTION 'notification forbidden' USING ERRCODE = '42501';
    END IF;
    SELECT c.customer_id, c.ticket_no, c.status::text
        INTO v_customer, v_ticket, v_status FROM public.complaints c
        WHERE c.id = p_id AND c.created_at = p_created;
    INSERT INTO public.notification_outbox
        (complaint_id, complaint_created_at, recipient_customer_id, kind, payload)
        VALUES (p_id, p_created, v_customer, p_kind,
            jsonb_build_object('ticket_no', v_ticket, 'status', v_status))
        RETURNING id INTO v_id;
    INSERT INTO public.notifications
        (recipient_customer_id, complaint_id, complaint_created_at, kind, payload)
        VALUES (v_customer, p_id, p_created, p_kind,
            jsonb_build_object('ticket_no', v_ticket, 'status', v_status));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.reopen_complaint(p_id uuid, p_created timestamptz)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_complaint complaints%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'customer'
       OR NOT app_security.can_read_complaint_id(p_id, p_created) THEN
        RETURN false;
    END IF;
    SELECT * INTO v_complaint FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR v_complaint.status <> 'resolved'
       OR v_complaint.resolved_at < now() - interval '14 days'
       OR v_complaint.resolved_at IS NULL OR v_complaint.reopened_count >= 3 THEN
        RETURN false;
    END IF;
    UPDATE public.complaints SET status = 'reopened', resolved_at = NULL,
        reopened_count = reopened_count + 1, updated_at = now()
        WHERE id = p_id AND created_at = p_created;
    INSERT INTO public.complaint_events (complaint_id, complaint_created_at,
        event_type, from_status, to_status, actor_customer_id)
        VALUES (p_id, p_created, 'reopened', 'resolved', 'reopened',
            app_security.actor_id());
    PERFORM app_security.queue_customer_notice(p_id, p_created, 'reopened');
    INSERT INTO public.audit_logs (action, resource_type, resource_id,
        complaint_id, complaint_created_at)
        VALUES ('customer_reopened', 'complaint', p_id, p_id, p_created);
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.submit_csat(p_id uuid, p_created timestamptz,
    p_score smallint, p_comment text) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_complaint complaints%ROWTYPE;
BEGIN
    IF app_security.actor_scope() <> 'customer'
       OR NOT app_security.can_read_complaint_id(p_id, p_created)
       OR p_score NOT BETWEEN 1 AND 5 OR length(coalesce(p_comment, '')) > 1000 THEN
        RETURN false;
    END IF;
    SELECT * INTO v_complaint FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR v_complaint.resolved_at IS NULL
       OR v_complaint.resolved_at < now() - interval '30 days'
       OR EXISTS (SELECT 1 FROM public.csat_responses
          WHERE complaint_id = p_id AND complaint_created_at = p_created
            AND customer_id = app_security.actor_id()) THEN
        RETURN false;
    END IF;
    INSERT INTO public.csat_responses
        (complaint_id, complaint_created_at, customer_id, score, comment)
        VALUES (p_id, p_created, app_security.actor_id(), p_score,
            nullif(btrim(p_comment), ''));
    INSERT INTO public.audit_logs (action, resource_type, resource_id,
        complaint_id, complaint_created_at)
        VALUES ('csat', 'complaint', p_id, p_id, p_created);
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.staff_change_status(p_id uuid, p_created timestamptz,
    p_status complaint_status) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_complaint complaints%ROWTYPE;
BEGIN
    IF NOT app_security.has_permission('complaints.change_status') OR
       NOT app_security.can_read_complaint_id(p_id, p_created) OR
       p_status NOT IN ('assigned', 'in_review', 'resolved', 'transferred') THEN
        RETURN false;
    END IF;
    SELECT * INTO v_complaint FROM public.complaints
        WHERE id = p_id AND created_at = p_created FOR UPDATE;
    IF NOT FOUND OR v_complaint.status = p_status OR
       (v_complaint.status = 'resolved' AND p_status <> 'in_review') THEN
        RETURN false;
    END IF;
    UPDATE public.complaints SET status = p_status,
        resolved_at = CASE WHEN p_status = 'resolved' THEN now() ELSE NULL END,
        updated_at = now() WHERE id = p_id AND created_at = p_created;
    INSERT INTO public.complaint_events
        (complaint_id, complaint_created_at, event_type, from_status, to_status,
            actor_staff_id) VALUES (p_id, p_created, 'status_changed',
            v_complaint.status, p_status, app_security.actor_id());
    PERFORM app_security.queue_customer_notice(p_id, p_created, 'status_changed');
    INSERT INTO public.audit_logs (actor_staff_id, action, resource_type,
        resource_id, complaint_id, complaint_created_at, before_state, after_state)
        VALUES (app_security.actor_id(), 'status_changed', 'complaint', p_id,
            p_id, p_created, jsonb_build_object('status', v_complaint.status),
            jsonb_build_object('status', p_status));
    RETURN true;
END;
$function$;

-- A separate DB login has no direct table privileges; only the worker can
-- read recipients or mark scan verdicts. An unconfigured scanner leaves
-- attachments pending, never clean by default.
DO $block$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
        CREATE ROLE app_worker NOLOGIN;
    END IF;
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO app_worker', current_database());
END $block$;
GRANT USAGE ON SCHEMA app_security TO app_worker;

CREATE FUNCTION app_security.claim_notices(p_limit integer DEFAULT 20)
RETURNS TABLE (notice_id uuid, kind text, payload jsonb, mobile text, email text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 100 THEN RETURN; END IF;
    RETURN QUERY WITH claim AS (
        SELECT o.id FROM public.notification_outbox o
        WHERE o.state IN ('queued', 'processing') AND o.attempts < 10
          AND o.next_attempt_at <= now()
        ORDER BY o.next_attempt_at, o.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    ), updated AS (
        UPDATE public.notification_outbox o SET state = 'processing',
            attempts = o.attempts + 1, next_attempt_at = now() + interval '5 minutes'
            FROM claim WHERE o.id = claim.id
        RETURNING o.id, o.kind, o.payload, o.recipient_customer_id
    ) SELECT u.id, u.kind, u.payload, c.mobile, c.email
        FROM updated u JOIN public.customers c ON c.id = u.recipient_customer_id;
END;
$function$;

CREATE FUNCTION app_security.finish_notice(p_id uuid, p_state text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_state NOT IN ('sent', 'pending_provider', 'failed', 'retry') THEN RETURN false; END IF;
    UPDATE public.notification_outbox SET
        state = CASE WHEN p_state = 'retry' THEN
            CASE WHEN attempts >= 10 THEN 'failed' ELSE 'queued' END ELSE p_state END,
        next_attempt_at = CASE WHEN p_state = 'retry' THEN
            now() + make_interval(secs => LEAST(3600, (2 ^ attempts)::integer * 30))
            ELSE next_attempt_at END,
        processed_at = CASE WHEN p_state = 'retry' THEN NULL ELSE now() END
        WHERE id = p_id AND state = 'processing';
    RETURN FOUND;
END;
$function$;

CREATE FUNCTION app_security.pending_scans(p_limit integer DEFAULT 20)
RETURNS TABLE (upload_id uuid, storage_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT u.id, u.storage_key FROM public.attachment_uploads u
    JOIN public.attachments a ON a.upload_id = u.id
    WHERE session_user = 'app_worker' AND app_security.actor_scope() = 'worker'
      AND p_limit BETWEEN 1 AND 100 AND u.status = 'uploaded'
      AND a.scan_status = 'pending'
    ORDER BY u.finalized_at, u.id LIMIT p_limit
$function$;

CREATE FUNCTION app_security.record_scan_verdict(p_id uuid, p_verdict text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_upload attachment_uploads%ROWTYPE;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_verdict NOT IN ('clean','infected','error') THEN RETURN false; END IF;
    SELECT * INTO v_upload FROM public.attachment_uploads
        WHERE id = p_id AND status = 'uploaded' FOR UPDATE;
    IF NOT FOUND THEN RETURN false; END IF;
    UPDATE public.attachments SET scan_status = p_verdict::attachment_scan_status
        WHERE upload_id = p_id AND scan_status = 'pending';
    RETURN FOUND;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.replace_user_access(uuid, jsonb),
    app_security.deactivate_staff(uuid), app_security.require_staff_totp(uuid, boolean),
    app_security.require_global_totp(boolean), app_security.guest_ticket_status(text, bytea),
    app_security.audit_customer_action(uuid, timestamptz, text),
    app_security.queue_customer_notice(uuid, timestamptz, text),
    app_security.reopen_complaint(uuid, timestamptz),
    app_security.submit_csat(uuid, timestamptz, smallint, text),
    app_security.staff_change_status(uuid, timestamptz, complaint_status)
    TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.claim_notices(integer),
    app_security.finish_notice(uuid, text),
    app_security.pending_scans(integer),
    app_security.record_scan_verdict(uuid, text) TO app_worker;
