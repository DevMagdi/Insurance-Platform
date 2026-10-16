-- Phase 8: RLS at scale.
--
-- EXPLAIN ANALYZE of the staff queue / dashboard count showed the original
-- complaints_read policy calling the SECURITY DEFINER can_read_complaint() once
-- PER ROW (it cannot be inlined), each call running ~8 catalog sub-queries:
-- 3.3 s for 5,092 visible rows, i.e. O(rows) privilege checks. At millions of
-- complaints this is the platform's first bottleneck.
--
-- The rewritten policy evaluates the actor's privileges ONCE per statement:
-- every helper is wrapped in a scalar sub-select, which PostgreSQL executes as
-- an InitPlan, and the per-row predicate becomes plain column comparisons
-- against those parameters. Semantics are identical to staff_scope_matches()
-- (permission + active staff + company / insurance-type / ownership scope,
-- deny overrides first); the RLS and pen-test suites prove it through direct
-- PostgreSQL and PgBouncer. Expand-only: helpers are new, the policy is
-- replaced atomically inside the migration transaction.

CREATE FUNCTION app_security.rls_complaint_mode() RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() = 'customer' THEN
        RETURN 'customer';
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

-- NULL = every company; '{}' = none.
CREATE FUNCTION app_security.rls_scope_companies() RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE
        WHEN EXISTS (SELECT 1 FROM public.user_company_scope
                     WHERE user_id = app_security.actor_id() AND all_companies) THEN NULL
        ELSE COALESCE((SELECT array_agg(company_id) FROM public.user_company_scope
                       WHERE user_id = app_security.actor_id() AND company_id IS NOT NULL),
                      '{}'::uuid[])
    END
$function$;

-- NULL = every insurance type (no restriction rows).
CREATE FUNCTION app_security.rls_scope_types() RETURNS public.insurance_type[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT array_agg(insurance_type) FROM public.user_insurance_type_scope
    WHERE user_id = app_security.actor_id()
$function$;

CREATE FUNCTION app_security.rls_scope_level() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT level::text FROM public.user_ownership_level WHERE user_id = app_security.actor_id()
$function$;

CREATE FUNCTION app_security.rls_scope_teams() RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT COALESCE(array_agg(team_id), '{}'::uuid[]) FROM public.team_members
    WHERE user_id = app_security.actor_id()
$function$;

REVOKE ALL ON FUNCTION app_security.rls_complaint_mode() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.rls_scope_companies() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.rls_scope_types() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.rls_scope_level() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.rls_scope_teams() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.rls_complaint_mode() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.rls_scope_companies() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.rls_scope_types() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.rls_scope_level() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.rls_scope_teams() TO app_runtime;

DROP POLICY complaints_read ON public.complaints;
CREATE POLICY complaints_read ON public.complaints FOR SELECT TO app_runtime USING (
    CASE (SELECT app_security.rls_complaint_mode())
        WHEN 'customer' THEN customer_id = (SELECT app_security.actor_id())
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
