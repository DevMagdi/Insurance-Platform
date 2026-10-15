-- Materialized daily rollup for analytics. The view is owned by the migration
-- role and has NO app_runtime grant: aggregates are only reachable through the
-- scope-checking reader below, which re-applies company/type/ownership rules on
-- every row. The live scoped query remains the authoritative fallback.
CREATE TABLE analytics_rollup_state (
    id boolean PRIMARY KEY DEFAULT true,
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    refreshed_rows integer,
    CONSTRAINT ck_rollup_singleton CHECK (id)
);
INSERT INTO analytics_rollup_state(id) VALUES (true) ON CONFLICT (id) DO NOTHING;
ALTER TABLE analytics_rollup_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE analytics_rollup_state FORCE ROW LEVEL SECURITY;
CREATE POLICY analytics_rollup_state_read ON analytics_rollup_state FOR SELECT TO app_runtime
    USING (app_security.is_active_staff()
        AND app_security.has_permission('analytics.view'));
-- Freshness metadata only; contains no ticket, customer or company data.
GRANT SELECT ON analytics_rollup_state TO app_runtime;

-- Nullable dimensions use an all-zero sentinel so the unique index (and thus a
-- future CONCURRENTLY refresh) stays valid.
CREATE MATERIALIZED VIEW public.analytics_daily AS
SELECT
    date_trunc('day', c.created_at AT TIME ZONE 'UTC')::date AS day,
    c.company_id,
    c.insurance_type::text AS insurance_type,
    c.priority::text AS priority,
    COALESCE(c.category_id, '00000000-0000-0000-0000-000000000000'::uuid) AS category_id,
    c.channel::text AS channel,
    c.status::text AS status,
    COALESCE(c.assigned_agent_id, '00000000-0000-0000-0000-000000000000'::uuid) AS agent_id,
    COALESCE(c.team_id, '00000000-0000-0000-0000-000000000000'::uuid) AS team_id,
    (c.sla_breached_at IS NOT NULL) AS sla_breached,
    (c.response_breached_at IS NOT NULL) AS response_breached,
    (c.reopened_count > 0) AS reopened,
    count(*) AS total,
    count(*) FILTER (WHERE c.resolved_at IS NOT NULL) AS resolved_count,
    COALESCE(sum(EXTRACT(EPOCH FROM (c.resolved_at - c.created_at)) / 3600)
        FILTER (WHERE c.resolved_at IS NOT NULL), 0)::numeric AS resolution_hours_sum,
    COALESCE(sum(c.csat_score) FILTER (WHERE c.csat_score IS NOT NULL), 0)::numeric AS csat_sum,
    count(*) FILTER (WHERE c.csat_score IS NOT NULL) AS csat_count,
    count(*) FILTER (WHERE c.first_response_at IS NOT NULL) AS first_response_count,
    COALESCE(sum(EXTRACT(EPOCH FROM (c.first_response_at - c.created_at)) / 3600)
        FILTER (WHERE c.first_response_at IS NOT NULL), 0)::numeric AS first_response_hours_sum
FROM public.complaints c
GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12;
CREATE UNIQUE INDEX ux_analytics_daily ON public.analytics_daily
    (day, company_id, insurance_type, priority, category_id, channel, status,
     agent_id, team_id, sla_breached, response_breached, reopened);

CREATE FUNCTION app_security.analytics_rollup_refresh() RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_rows integer;
BEGIN
    IF session_user <> 'app_worker' OR app_security.actor_scope() <> 'worker' THEN
        RETURN -1;
    END IF;
    UPDATE public.analytics_rollup_state SET last_attempt_at = now() WHERE id;
    BEGIN
        REFRESH MATERIALIZED VIEW public.analytics_daily;
    EXCEPTION WHEN OTHERS THEN
        UPDATE public.analytics_rollup_state
            SET last_error = left(SQLERRM, 300) WHERE id;
        RETURN -1;
    END;
    SELECT count(*) INTO v_rows FROM public.analytics_daily;
    UPDATE public.analytics_rollup_state
        SET last_success_at = now(), last_error = NULL, refreshed_rows = v_rows
        WHERE id;
    RETURN v_rows;
END;
$function$;

CREATE FUNCTION app_security.analytics_rollup_fresh(p_within interval DEFAULT '48 hours')
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public AS $function$
    SELECT app_security.is_active_staff()
        AND app_security.has_permission('analytics.view')
        AND EXISTS (SELECT 1 FROM public.analytics_rollup_state s
            WHERE s.last_success_at IS NOT NULL AND s.last_success_at > now() - p_within)
$function$;

-- Scope-safe reader. Whole UTC days only: the caller must pass midnight-to-midnight
-- boundaries, because a day-granular rollup cannot answer a partial-day window.
CREATE FUNCTION app_security.analytics_daily_scoped(
    p_from timestamptz, p_to timestamptz,
    p_status text DEFAULT NULL, p_company uuid DEFAULT NULL,
    p_type text DEFAULT NULL, p_priority text DEFAULT NULL,
    p_category uuid DEFAULT NULL, p_channel text DEFAULT NULL,
    p_agent uuid DEFAULT NULL)
RETURNS TABLE (day date, company_id uuid, insurance_type text, priority text,
    category_id uuid, channel text, status text, agent_id uuid, team_id uuid,
    sla_breached boolean, response_breached boolean, reopened boolean,
    total bigint, resolved_count bigint, resolution_hours_sum numeric,
    csat_sum numeric, csat_count bigint, first_response_count bigint,
    first_response_hours_sum numeric)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NOT app_security.is_active_staff()
       OR NOT app_security.has_permission('analytics.view')
       OR NOT app_security.has_permission('complaints.view') THEN
        RETURN;
    END IF;
    RETURN QUERY
    SELECT m.day, m.company_id, m.insurance_type, m.priority, m.category_id,
        m.channel, m.status, m.agent_id, m.team_id, m.sla_breached,
        m.response_breached, m.reopened, m.total, m.resolved_count,
        m.resolution_hours_sum, m.csat_sum, m.csat_count,
        m.first_response_count, m.first_response_hours_sum
    FROM public.analytics_daily m
    WHERE m.day >= (p_from AT TIME ZONE 'UTC')::date
      AND m.day < (p_to AT TIME ZONE 'UTC')::date
      AND app_security.has_company_scope(m.company_id)
      AND (NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                       WHERE t.user_id = app_security.actor_id())
           OR EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                      WHERE t.user_id = app_security.actor_id()
                        AND t.insurance_type = m.insurance_type::insurance_type))
      -- Fail closed: no ownership row means no rows, never "everything".
      AND EXISTS (SELECT 1 FROM public.user_ownership_level o
                  WHERE o.user_id = app_security.actor_id() AND (
                      o.level = 'all'
                      OR (o.level = 'own' AND m.agent_id = app_security.actor_id())
                      OR (o.level = 'team' AND EXISTS (
                            SELECT 1 FROM public.team_members tm
                            WHERE tm.team_id = m.team_id
                              AND tm.user_id = app_security.actor_id()))))
      AND (p_status IS NULL OR m.status = p_status)
      AND (p_company IS NULL OR m.company_id = p_company)
      AND (p_type IS NULL OR m.insurance_type = p_type)
      AND (p_priority IS NULL OR m.priority = p_priority)
      AND (p_category IS NULL OR m.category_id = p_category)
      AND (p_channel IS NULL OR m.channel = p_channel)
      AND (p_agent IS NULL OR m.agent_id = p_agent);
END;
$function$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA app_security FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
    app_security.analytics_rollup_fresh(interval),
    app_security.analytics_daily_scoped(timestamptz,timestamptz,text,uuid,text,text,
        uuid,text,uuid) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.analytics_rollup_refresh() TO app_worker;
