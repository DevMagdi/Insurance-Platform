-- Private async CSV artifacts: worker can ONLY claim, obtain re-scoped rows
-- and finish jobs through these functions. Downloads recheck EVERY exported
-- ticket against current grants/company/type/ownership, not a stale scope hash.
ALTER TABLE export_jobs ADD COLUMN attempts integer NOT NULL DEFAULT 0;
ALTER TABLE export_jobs ADD COLUMN started_at timestamptz;
CREATE INDEX ix_exports_dispatch ON export_jobs (created_at, id)
    WHERE status IN ('queued','running');
CREATE TABLE export_job_rows (
    job_id uuid NOT NULL REFERENCES export_jobs(id) ON DELETE CASCADE,
    complaint_id uuid NOT NULL,
    complaint_created_at timestamptz NOT NULL,
    PRIMARY KEY (job_id, complaint_id),
    FOREIGN KEY (complaint_id, complaint_created_at)
        REFERENCES complaints (id, created_at) ON DELETE CASCADE
);
ALTER TABLE export_job_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE export_job_rows FORCE ROW LEVEL SECURITY;
-- No direct app_runtime or app_worker table privileges or RLS policies.

CREATE FUNCTION app_security.request_export(p_kind text, p_filters jsonb) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_start timestamptz; v_end timestamptz;
BEGIN
    IF NOT app_security.is_active_staff() OR p_kind NOT IN ('complaints','analytics')
       OR NOT app_security.has_permission('complaints.view')
       OR (p_kind = 'complaints' AND NOT app_security.has_permission('complaints.export'))
       OR (p_kind = 'analytics' AND NOT
           (app_security.has_permission('analytics.view')
            AND app_security.has_permission('analytics.export')))
       OR jsonb_typeof(p_filters) <> 'object'
       OR p_filters - ARRAY['company_id','insurance_type','status',
           'assigned_agent_id','priority','category_id','channel','date_from',
           'date_to','search','ticket_no','sort_by','sort_dir','page','page_size']
           <> '{}'::jsonb
       OR p_filters->>'date_from' IS NULL OR p_filters->>'date_to' IS NULL THEN
        RAISE EXCEPTION 'export request forbidden' USING ERRCODE = '42501';
    END IF;
    v_start := (p_filters->>'date_from')::timestamptz;
    v_end := (p_filters->>'date_to')::timestamptz;
    IF v_end < v_start OR v_end - v_start > interval '366 days' THEN
        RAISE EXCEPTION 'export window must be at most 366 days' USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.export_jobs(owner_staff_id,kind,filters)
        VALUES (app_security.actor_id(),p_kind,p_filters) RETURNING id INTO v_id;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'export_requested','export_job',v_id,
            jsonb_build_object('kind',p_kind));
    RETURN v_id;
END;
$function$;

CREATE FUNCTION app_security.claim_exports(p_limit integer DEFAULT 10)
RETURNS TABLE (job_id uuid, attempt integer, kind text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 20 THEN RETURN; END IF;
    RETURN QUERY WITH claim AS (
        SELECT j.id FROM public.export_jobs j
        WHERE (j.status = 'queued' OR (j.status = 'running'
                AND j.started_at < now() - interval '5 minutes'))
          AND j.attempts < 3
        ORDER BY j.created_at, j.id LIMIT p_limit FOR UPDATE SKIP LOCKED
    ), updated AS (
        UPDATE public.export_jobs j SET status = 'running',
            started_at = now(), attempts = j.attempts + 1
            FROM claim WHERE claim.id = j.id
            RETURNING j.id, j.attempts, j.kind
    ) SELECT u.id, u.attempts, u.kind FROM updated u;
END;
$function$;

CREATE FUNCTION app_security.export_rows(p_job uuid, p_attempt integer)
RETURNS TABLE (
    complaint_id uuid, complaint_created_at timestamptz,
    ticket_no text, created_at timestamptz, company_id uuid,
    insurance_type text, status text, subject text, policy_number text,
    sla_breached boolean, resolved_at timestamptz, csat_score integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_owner uuid; v_kind text; v_filters jsonb;
    v_old_scope text := current_setting('app.scope',true);
    v_old_id text := current_setting('app.user_id',true);
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker' THEN
        RAISE EXCEPTION 'worker access required' USING ERRCODE = '42501';
    END IF;
    SELECT j.owner_staff_id, j.kind, j.filters INTO v_owner,v_kind,v_filters
        FROM public.export_jobs j WHERE j.id = p_job AND j.status = 'running'
            AND j.attempts = p_attempt FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'export claim expired' USING ERRCODE = '42501'; END IF;
    PERFORM set_config('app.scope','staff',true);
    PERFORM set_config('app.user_id',v_owner::text,true);
    IF NOT app_security.has_permission('complaints.view')
       OR (v_kind = 'complaints' AND NOT app_security.has_permission('complaints.export'))
       OR (v_kind = 'analytics' AND NOT
           (app_security.has_permission('analytics.export')
            AND app_security.has_permission('analytics.view'))) THEN
        RAISE EXCEPTION 'export access revoked' USING ERRCODE = '42501';
    END IF;
    RETURN QUERY SELECT c.id, c.created_at,
        CASE WHEN v_kind = 'complaints' THEN c.ticket_no ELSE NULL::text END,
        c.created_at, c.company_id, c.insurance_type::text,
        c.status::text,
        CASE WHEN v_kind = 'complaints' THEN c.subject ELSE NULL::text END,
        CASE WHEN v_kind = 'complaints' THEN c.policy_number ELSE NULL::text END,
        c.sla_breached_at IS NOT NULL, c.resolved_at, c.csat_score::integer
    FROM public.complaints c
    WHERE app_security.can_read_complaint(c.company_id,c.insurance_type,
              c.assigned_agent_id,c.team_id,c.customer_id)
      AND c.created_at >= (v_filters->>'date_from')::timestamptz
      AND c.created_at <= (v_filters->>'date_to')::timestamptz
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

CREATE FUNCTION app_security.finish_export(
    p_job uuid, p_attempt integer, p_success boolean, p_key text,
    p_manifest jsonb DEFAULT '[]'::jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_owner uuid; v_kind text;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_attempt NOT BETWEEN 1 AND 3 OR
       (p_success AND (p_key IS DISTINCT FROM
           'exports/' || p_job::text || '/' || p_attempt::text || '.csv'
           OR jsonb_typeof(p_manifest) <> 'array'
           OR jsonb_array_length(p_manifest) > 1000)) THEN RETURN false; END IF;
    UPDATE public.export_jobs j SET status = CASE WHEN p_success THEN 'completed' ELSE 'failed' END,
        private_storage_key = CASE WHEN p_success THEN p_key ELSE NULL END,
        completed_at = now(), expires_at = CASE WHEN p_success
            THEN now() + interval '15 minutes' ELSE NULL END
        WHERE j.id = p_job AND j.status = 'running' AND j.attempts = p_attempt
        RETURNING j.owner_staff_id, j.kind INTO v_owner,v_kind;
    IF NOT FOUND THEN RETURN false; END IF;
    IF p_success THEN
        INSERT INTO public.export_job_rows(job_id,complaint_id,complaint_created_at)
            SELECT p_job, (item->>'id')::uuid, (item->>'created_at')::timestamptz
            FROM jsonb_array_elements(p_manifest) item;
        INSERT INTO public.notifications(recipient_staff_id,kind,payload)
            VALUES (v_owner,'export_ready',jsonb_build_object('job_id',p_job));
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (NULL,CASE WHEN p_success THEN 'export_completed' ELSE 'export_failed' END,
            'export_job',p_job,jsonb_build_object('kind',v_kind,
                'count',CASE WHEN p_success THEN jsonb_array_length(p_manifest) ELSE 0 END));
    RETURN true;
END;
$function$;

CREATE FUNCTION app_security.authorize_export_download(p_job uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_job export_jobs%ROWTYPE;
BEGIN
    IF NOT app_security.is_active_staff() THEN RETURN NULL; END IF;
    SELECT * INTO v_job FROM public.export_jobs j
        WHERE j.id = p_job AND j.owner_staff_id = app_security.actor_id()
            AND j.status = 'completed' AND j.expires_at > now() FOR SHARE;
    IF NOT FOUND OR NOT app_security.has_permission('complaints.view')
       OR (v_job.kind = 'complaints' AND NOT app_security.has_permission('complaints.export'))
       OR (v_job.kind = 'analytics' AND NOT
           (app_security.has_permission('analytics.view')
            AND app_security.has_permission('analytics.export')))
       OR EXISTS (SELECT 1 FROM public.export_job_rows r WHERE r.job_id = p_job
            AND NOT app_security.can_read_complaint_id(r.complaint_id,
                r.complaint_created_at)) THEN RETURN NULL;
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'export_downloaded','export_job',p_job,
            jsonb_build_object('kind',v_job.kind));
    RETURN v_job.private_storage_key;
END;
$function$;

-- Credential/scope/role changes instantly expire previously created artifacts.
CREATE OR REPLACE FUNCTION app_security.notify_access_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    UPDATE public.export_jobs SET status = 'expired', expires_at = now()
        WHERE owner_staff_id = NEW.target_user_id
            AND status IN ('queued','running','completed');
    INSERT INTO public.notifications(recipient_staff_id, kind, payload)
        VALUES (NEW.target_user_id, 'access_changed',
            jsonb_build_object('change_type', NEW.change_type,
                               'changed_at', NEW.created_at));
    RETURN NEW;
END;
$function$;

CREATE FUNCTION app_security.claim_export_cleanup(p_limit integer DEFAULT 20)
RETURNS TABLE (job_id uuid, storage_key text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_limit NOT BETWEEN 1 AND 50 THEN RETURN; END IF;
    RETURN QUERY WITH expired AS (
        SELECT j.id FROM public.export_jobs j WHERE j.private_storage_key IS NOT NULL
          AND (j.status = 'expired' OR
               (j.status = 'completed' AND j.expires_at <= now()))
        ORDER BY j.created_at LIMIT p_limit FOR UPDATE SKIP LOCKED
    ), marked AS (
        UPDATE public.export_jobs j SET status = 'expired'
        FROM expired WHERE expired.id = j.id
        RETURNING j.id,j.private_storage_key
    ) SELECT m.id,m.private_storage_key FROM marked m;
END;
$function$;

CREATE FUNCTION app_security.finish_export_cleanup(p_job uuid, p_key text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_key IS NULL THEN RETURN false; END IF;
    UPDATE public.export_jobs SET private_storage_key = NULL
        WHERE id = p_job AND status = 'expired' AND private_storage_key = p_key;
    RETURN FOUND;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.request_export(text,jsonb),
    app_security.authorize_export_download(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.claim_exports(integer),
    app_security.export_rows(uuid,integer),
    app_security.finish_export(uuid,integer,boolean,text,jsonb),
    app_security.claim_export_cleanup(integer),
    app_security.finish_export_cleanup(uuid,text) TO app_worker;
