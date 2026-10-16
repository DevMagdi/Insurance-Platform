-- CSV, Excel and PDF artifacts share one job pipeline. The format is fixed at
-- request time, the worker writes the matching extension and the download
-- endpoint derives the media type from the authorized (server-generated) key.
ALTER TABLE export_jobs ADD COLUMN format text NOT NULL DEFAULT 'csv';
ALTER TABLE export_jobs ADD CONSTRAINT ck_export_format
    CHECK (format IN ('csv','xlsx','pdf'));

DROP FUNCTION app_security.request_export(text, jsonb);
CREATE FUNCTION app_security.request_export(
    p_kind text, p_filters jsonb, p_format text DEFAULT 'csv')
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_id uuid; v_start timestamptz; v_end timestamptz;
BEGIN
    IF NOT app_security.is_active_staff() OR p_kind NOT IN ('complaints','analytics')
       OR COALESCE(p_format,'csv') NOT IN ('csv','xlsx','pdf')
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
    INSERT INTO public.export_jobs(owner_staff_id,kind,filters,format)
        VALUES (app_security.actor_id(),p_kind,p_filters,COALESCE(p_format,'csv'))
        RETURNING id INTO v_id;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (app_security.actor_id(),'export_requested','export_job',v_id,
            jsonb_build_object('kind',p_kind,'format',COALESCE(p_format,'csv')));
    RETURN v_id;
END;
$function$;

CREATE OR REPLACE FUNCTION app_security.authorize_export_download(p_job uuid) RETURNS text
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
            jsonb_build_object('kind',v_job.kind,'format',v_job.format));
    RETURN v_job.private_storage_key;
END;
$function$;

DROP FUNCTION app_security.claim_exports(integer);
CREATE FUNCTION app_security.claim_exports(p_limit integer DEFAULT 10)
RETURNS TABLE (job_id uuid, attempt integer, kind text, format text)
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
            RETURNING j.id, j.attempts, j.kind, j.format
    ) SELECT u.id, u.attempts, u.kind, u.format FROM updated u;
END;
$function$;

CREATE OR REPLACE FUNCTION app_security.finish_export(
    p_job uuid, p_attempt integer, p_success boolean, p_key text,
    p_manifest jsonb DEFAULT '[]'::jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
DECLARE v_owner uuid; v_kind text; v_format text;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker'
       OR p_attempt NOT BETWEEN 1 AND 3 OR
       (p_success AND (jsonb_typeof(p_manifest) <> 'array'
           OR jsonb_array_length(p_manifest) > 1000)) THEN RETURN false; END IF;
    SELECT j.format INTO v_format FROM public.export_jobs j WHERE j.id = p_job;
    IF v_format IS NULL THEN RETURN false; END IF;
    IF p_success AND p_key IS DISTINCT FROM
        ('exports/' || p_job::text || '/' || p_attempt::text || '.' || v_format) THEN
        RETURN false;
    END IF;
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
            VALUES (v_owner,'export_ready',jsonb_build_object('job_id',p_job,
                'format',v_format));
    END IF;
    INSERT INTO public.audit_logs(actor_staff_id,action,resource_type,resource_id,after_state)
        VALUES (NULL,CASE WHEN p_success THEN 'export_completed' ELSE 'export_failed' END,
            'export_job',p_job,jsonb_build_object('kind',v_kind,'format',v_format,
                'count',CASE WHEN p_success THEN jsonb_array_length(p_manifest) ELSE 0 END));
    RETURN true;
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
-- The format is not a secret, but the column was added after the column-level
-- grant in revision 002, so it must be granted explicitly.
GRANT SELECT (format) ON export_jobs TO app_runtime;
GRANT EXECUTE ON FUNCTION
    app_security.request_export(text,jsonb,text),
    app_security.authorize_export_download(uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION
    app_security.claim_exports(integer),
    app_security.finish_export(uuid,integer,boolean,text,jsonb) TO app_worker;
