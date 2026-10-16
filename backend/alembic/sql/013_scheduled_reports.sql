-- Scheduled email reports. Schedules are owner-scoped; every execution
-- reauthorizes the CURRENT grants/company scope of the owner and records an
-- auditable run. Recipients are addresses, never an authority: rows are
-- re-scoped per row at render time exactly like async exports.
ALTER TABLE report_schedules ADD COLUMN format text NOT NULL DEFAULT 'csv';
ALTER TABLE report_schedules ADD CONSTRAINT ck_report_format
    CHECK (format IN ('csv','xlsx'));
ALTER TABLE report_schedules ADD COLUMN last_run_at timestamptz;
ALTER TABLE report_schedules ADD COLUMN last_state text;
ALTER TABLE report_schedules ADD COLUMN claim_expires_at timestamptz;
ALTER TABLE report_schedules ADD CONSTRAINT ck_report_last_state
    CHECK (last_state IS NULL OR last_state IN ('sent','empty','failed','skipped'));

CREATE TABLE report_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schedule_id uuid NOT NULL REFERENCES report_schedules(id) ON DELETE CASCADE,
    owner_staff_id uuid NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
    attempt smallint NOT NULL DEFAULT 1 CHECK (attempt BETWEEN 1 AND 3),
    state text NOT NULL DEFAULT 'running'
        CHECK (state IN ('running','sent','empty','failed','skipped')),
    row_count integer NOT NULL DEFAULT 0 CHECK (row_count BETWEEN 0 AND 1000),
    error_detail text,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX ix_report_runs_schedule ON report_runs (schedule_id, started_at DESC, id DESC);
ALTER TABLE report_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE report_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY report_runs_read ON report_runs FOR SELECT TO app_runtime
    USING (app_security.is_active_staff()
        AND owner_staff_id = app_security.actor_id()
        AND app_security.has_permission('reports.manage'));
-- No INSERT/UPDATE/DELETE policy: only the guarded definer functions write runs.

CREATE FUNCTION app_security.save_report_schedule(p_id uuid, p_data jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_owner uuid; v_emails text[]; v_next timestamptz;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('reports.manage')
       OR NOT app_security.has_permission('analytics.view')
       OR NOT app_security.has_permission('analytics.export')
       OR jsonb_typeof(p_data) <> 'object'
       OR p_data - ARRAY['name','filters','cron_expression','time_zone',
            'recipient_emails','format','next_run_at','is_active'] <> '{}'::jsonb
       OR length(btrim(COALESCE(p_data->>'name',''))) NOT BETWEEN 2 AND 80
       OR COALESCE(p_data->>'cron_expression','') !~ '^[0-9*,/ -]{9,60}$'
       OR p_data->>'time_zone' IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_timezone_names z WHERE z.name = p_data->>'time_zone')
       OR COALESCE(p_data->>'format','csv') NOT IN ('csv','xlsx')
       OR jsonb_typeof(p_data->'is_active') <> 'boolean'
       OR COALESCE(jsonb_typeof(p_data->'recipient_emails'),'missing') <> 'array'
       OR COALESCE(jsonb_array_length(p_data->'recipient_emails'),0) NOT BETWEEN 1 AND 10
       OR jsonb_typeof(p_data->'filters') <> 'object'
       -- Explicit parentheses: the filter document is validated key by key.
       OR (p_data->'filters') - ARRAY['company_id','insurance_type','status',
            'assigned_agent_id','priority','category_id','channel','search',
            'ticket_no','window_days'] <> '{}'::jsonb
       OR (p_data->'filters'->>'window_days')::integer NOT BETWEEN 1 AND 366
       OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(p_data->'recipient_emails') AS addr
           WHERE addr !~ '^[^@ ]+@[^@ ]+\.[^@ ]+$' OR addr <> lower(addr)
              OR length(addr) > 254)
       OR (p_data->>'next_run_at')::timestamptz IS NULL
       OR (p_data->>'next_run_at')::timestamptz < now() - interval '1 day'
       OR (p_data->>'next_run_at')::timestamptz > now() + interval '400 days' THEN
        RAISE EXCEPTION 'report schedule forbidden or invalid' USING ERRCODE = '42501';
    END IF;
    SELECT array_agg(addr ORDER BY addr) INTO v_emails
        FROM (SELECT DISTINCT jsonb_array_elements_text(p_data->'recipient_emails') AS addr) x;
    v_next := (p_data->>'next_run_at')::timestamptz;
    IF p_id IS NULL THEN
        INSERT INTO public.report_schedules(owner_staff_id,name,filters,cron_expression,
            time_zone,recipient_emails,format,next_run_at,is_active)
        VALUES (app_security.actor_id(), btrim(p_data->>'name'), p_data->'filters',
            p_data->>'cron_expression', p_data->>'time_zone', v_emails,
            COALESCE(p_data->>'format','csv'), v_next, (p_data->>'is_active')::boolean)
        RETURNING id INTO v_id;
    ELSE
        SELECT owner_staff_id INTO v_owner FROM public.report_schedules
            WHERE id = p_id FOR UPDATE;
        IF NOT FOUND THEN RETURN NULL; END IF;
        IF v_owner <> app_security.actor_id() THEN
            RAISE EXCEPTION 'report schedule belongs to another user' USING ERRCODE = '42501';
        END IF;
        UPDATE public.report_schedules SET name = btrim(p_data->>'name'),
            filters = p_data->'filters', cron_expression = p_data->>'cron_expression',
            time_zone = p_data->>'time_zone', recipient_emails = v_emails,
            format = COALESCE(p_data->>'format','csv'), next_run_at = v_next,
            is_active = (p_data->>'is_active')::boolean, claim_expires_at = NULL
            WHERE id = p_id;
        v_id := p_id;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),
            CASE WHEN p_id IS NULL THEN 'report_schedule_created'
                 ELSE 'report_schedule_updated' END,
            'report_schedule', v_id, jsonb_build_object('format', p_data->>'format',
                'cron_expression', p_data->>'cron_expression',
                'window_days', p_data->'filters'->>'window_days',
                'recipients', COALESCE(array_length(v_emails,1),0),
                'active', (p_data->>'is_active')::boolean));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.delete_report_schedule(p_id uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_owner uuid;
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('reports.manage') THEN
        RAISE EXCEPTION 'report schedule forbidden' USING ERRCODE = '42501';
    END IF;
    DELETE FROM public.report_schedules WHERE id = p_id
        AND owner_staff_id = app_security.actor_id() RETURNING owner_staff_id INTO v_owner;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id)
        VALUES (app_security.actor_id(),'report_schedule_deleted','report_schedule',p_id);
    RETURN true;
END;
$function$;

-- Worker claim. Owners whose grants were revoked are deactivated instead of
-- receiving a report built from a stale authorization snapshot.
CREATE FUNCTION app_security.claim_due_reports(p_limit integer DEFAULT 10)
RETURNS TABLE (run_id uuid, schedule_id uuid, owner_staff_id uuid, name text,
    filters jsonb, format text, recipients text[], attempt integer,
    cron_expression text, time_zone text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_schedule report_schedules%ROWTYPE; v_run uuid; v_allowed boolean;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 20 THEN RETURN; END IF;
    FOR v_schedule IN
        SELECT s.* FROM public.report_schedules s
        WHERE s.is_active AND s.next_run_at IS NOT NULL AND s.next_run_at <= now()
          AND (s.claim_expires_at IS NULL OR s.claim_expires_at <= now())
        ORDER BY s.next_run_at, s.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    LOOP
        v_allowed := EXISTS (SELECT 1 FROM public.staff_users u
                WHERE u.id = v_schedule.owner_staff_id AND u.status = 'active')
            AND EXISTS (SELECT 1 FROM public.user_roles ur
                JOIN public.role_permissions rp ON rp.role_id = ur.role_id
                WHERE ur.user_id = v_schedule.owner_staff_id
                  AND rp.permission_code = 'reports.manage')
            AND EXISTS (SELECT 1 FROM public.user_roles ur
                JOIN public.role_permissions rp ON rp.role_id = ur.role_id
                WHERE ur.user_id = v_schedule.owner_staff_id
                  AND rp.permission_code = 'analytics.view')
            AND EXISTS (SELECT 1 FROM public.user_roles ur
                JOIN public.role_permissions rp ON rp.role_id = ur.role_id
                WHERE ur.user_id = v_schedule.owner_staff_id
                  AND rp.permission_code = 'analytics.export')
            AND NOT EXISTS (SELECT 1 FROM public.user_permission_overrides o
                WHERE o.user_id = v_schedule.owner_staff_id
                  AND o.permission_code IN ('reports.manage','analytics.view','analytics.export')
                  AND o.effect = 'deny')
            AND EXISTS (SELECT 1 FROM public.user_company_scope cs
                WHERE cs.user_id = v_schedule.owner_staff_id)
            AND EXISTS (SELECT 1 FROM public.user_ownership_level ow
                WHERE ow.user_id = v_schedule.owner_staff_id);
        IF NOT v_allowed THEN
            UPDATE public.report_schedules SET is_active = false, next_run_at = NULL,
                claim_expires_at = NULL, last_state = 'skipped'
                WHERE id = v_schedule.id;
            INSERT INTO public.report_runs(schedule_id, owner_staff_id, state, error_detail)
                VALUES (v_schedule.id, v_schedule.owner_staff_id, 'skipped',
                    'owner access revoked');
            INSERT INTO public.notifications(recipient_staff_id, kind, payload)
                VALUES (v_schedule.owner_staff_id, 'report_disabled',
                    jsonb_build_object('schedule_id', v_schedule.id,
                        'name', v_schedule.name));
            INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id)
                VALUES (NULL,'report_schedule_disabled','report_schedule',v_schedule.id);
            CONTINUE;
        END IF;
        INSERT INTO public.report_runs(schedule_id, owner_staff_id)
            VALUES (v_schedule.id, v_schedule.owner_staff_id) RETURNING id INTO v_run;
        UPDATE public.report_schedules SET claim_expires_at = now() + interval '10 minutes'
            WHERE id = v_schedule.id;
        run_id := v_run; schedule_id := v_schedule.id;
        owner_staff_id := v_schedule.owner_staff_id; name := v_schedule.name;
        filters := v_schedule.filters; format := v_schedule.format;
        recipients := v_schedule.recipient_emails; attempt := 1;
        cron_expression := v_schedule.cron_expression; time_zone := v_schedule.time_zone;
        RETURN NEXT;
    END LOOP;
END;
$function$;

-- Rows are produced under the OWNER's identity and re-checked per row; the
-- window is relative to the run, so a stale schedule cannot leak new tickets.
CREATE FUNCTION app_security.report_rows(p_schedule uuid)
RETURNS TABLE (ticket_no text, created_at timestamptz, company_id uuid,
    insurance_type text, status text, priority text, subject text,
    policy_number text, sla_due_at timestamptz, sla_breached boolean,
    resolved_at timestamptz, csat_score integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_owner uuid; v_filters jsonb; v_days integer; v_start timestamptz;
    v_old_scope text := current_setting('app.scope',true);
    v_old_id text := current_setting('app.user_id',true);
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker' THEN
        RAISE EXCEPTION 'worker access required' USING ERRCODE = '42501';
    END IF;
    SELECT s.owner_staff_id, s.filters INTO v_owner, v_filters
        FROM public.report_schedules s WHERE s.id = p_schedule FOR SHARE;
    IF v_owner IS NULL THEN RETURN; END IF;
    v_days := (v_filters->>'window_days')::integer;
    v_start := now() - make_interval(days => GREATEST(LEAST(v_days, 366), 1));
    PERFORM set_config('app.scope','staff',true);
    PERFORM set_config('app.user_id',v_owner::text,true);
    IF NOT app_security.has_permission('complaints.view')
       OR NOT app_security.has_permission('analytics.view')
       OR NOT app_security.has_permission('analytics.export') THEN
        RAISE EXCEPTION 'report access revoked' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY SELECT c.ticket_no, c.created_at, c.company_id,
        c.insurance_type::text, c.status::text, c.priority::text, c.subject,
        c.policy_number, c.sla_due_at, c.sla_breached_at IS NOT NULL,
        c.resolved_at, c.csat_score::integer
    FROM public.complaints c
    WHERE app_security.can_read_complaint(c.company_id,c.insurance_type,
              c.assigned_agent_id,c.team_id,c.customer_id)
      AND c.created_at >= v_start AND c.created_at <= now()
      AND (v_filters->>'company_id' IS NULL
           OR c.company_id = (v_filters->>'company_id')::uuid)
      AND (v_filters->>'insurance_type' IS NULL
           OR c.insurance_type = (v_filters->>'insurance_type')::insurance_type)
      AND (v_filters->>'status' IS NULL
           OR c.status = (v_filters->>'status')::complaint_status)
      AND (v_filters->>'priority' IS NULL
           OR c.priority = (v_filters->>'priority')::complaint_priority)
      AND (v_filters->>'category_id' IS NULL
           OR c.category_id = (v_filters->>'category_id')::uuid)
      AND (v_filters->>'assigned_agent_id' IS NULL
           OR c.assigned_agent_id = (v_filters->>'assigned_agent_id')::uuid)
      AND (v_filters->>'channel' IS NULL
           OR c.channel = (v_filters->>'channel')::communication_channel)
      AND (v_filters->>'ticket_no' IS NULL OR c.ticket_no = v_filters->>'ticket_no')
      AND (v_filters->>'search' IS NULL
           OR c.search_vector @@ plainto_tsquery('simple',v_filters->>'search'))
    ORDER BY c.created_at DESC,c.id LIMIT 1001;
    PERFORM set_config('app.scope',COALESCE(v_old_scope,''),true);
    PERFORM set_config('app.user_id',COALESCE(v_old_id,''),true);
END;
$function$;

-- Three consecutive delivery failures disable a schedule instead of retrying
-- forever. The next run time (cron evaluated by the worker) is passed in.
CREATE FUNCTION app_security.finish_report_run(
    p_run uuid, p_state text, p_row_count integer, p_next_run timestamptz,
    p_error text DEFAULT NULL)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_run report_runs%ROWTYPE; v_failures integer;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_state NOT IN ('sent','empty','failed')
       OR p_row_count IS NULL OR p_row_count NOT BETWEEN 0 AND 1000
       OR (p_state <> 'failed' AND p_row_count = 0 AND p_state <> 'empty')
       OR (p_error IS NOT NULL AND length(p_error) > 300) THEN
        RETURN false;
    END IF;
    UPDATE public.report_runs SET state = p_state, row_count = p_row_count,
        error_detail = p_error, completed_at = now()
        WHERE id = p_run AND state = 'running' RETURNING * INTO v_run;
    IF NOT FOUND THEN RETURN false; END IF;
    SELECT count(*) INTO v_failures FROM (
        SELECT r.state FROM public.report_runs r
        WHERE r.schedule_id = v_run.schedule_id
        ORDER BY r.started_at DESC, r.id DESC LIMIT 3
    ) recent WHERE recent.state = 'failed';
    UPDATE public.report_schedules SET last_run_at = now(), last_state = p_state,
        claim_expires_at = NULL,
        next_run_at = CASE WHEN p_state = 'failed' AND v_failures >= 3
            THEN NULL ELSE p_next_run END,
        is_active = CASE WHEN p_state = 'failed' AND v_failures >= 3
            THEN false ELSE is_active END
        WHERE id = v_run.schedule_id;
    IF p_state <> 'skipped' THEN
        INSERT INTO public.notifications(recipient_staff_id, kind, payload)
            VALUES (v_run.owner_staff_id, 'report_' || p_state,
                jsonb_build_object('schedule_id', v_run.schedule_id,
                    'row_count', p_row_count, 'error', p_error));
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (NULL,'report_run_' || p_state,'report_schedule',v_run.schedule_id,
            jsonb_build_object('run_id', p_run, 'rows', p_row_count));
    RETURN true;
END;
$function$;

-- An owner can ask for the next run immediately; the worker still reauthorizes
-- the owner and re-scopes every row when the report is actually produced.
CREATE FUNCTION app_security.run_report_schedule(p_id uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('reports.manage')
       OR NOT app_security.has_permission('analytics.export') THEN
        RAISE EXCEPTION 'report schedule forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.report_schedules SET next_run_at = now(), claim_expires_at = NULL
        WHERE id = p_id AND owner_staff_id = app_security.actor_id() AND is_active;
    IF NOT FOUND THEN RETURN false; END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id)
        VALUES (app_security.actor_id(),'report_schedule_queued','report_schedule',p_id);
    RETURN true;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT SELECT ON report_runs TO app_runtime;  -- Row policy stays owner-scoped.
GRANT EXECUTE ON FUNCTION
    app_security.save_report_schedule(uuid,jsonb),
    app_security.delete_report_schedule(uuid),
    app_security.run_report_schedule(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION
    app_security.claim_due_reports(integer),
    app_security.report_rows(uuid),
    app_security.finish_report_run(uuid,text,integer,timestamptz,text) TO app_worker;
