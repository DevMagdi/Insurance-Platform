-- Live staff notices for customer activity + lightweight directory revisions.
--
-- Part 1 (notices): when a customer creates a complaint or sends a message,
-- every staff member whose AccessScope covers the ticket and who holds
-- complaints.view receives a notification row immediately, so the staff
-- console bell and ticket stream reflect it without a page refresh. The
-- fan-out is SECURITY DEFINER and re-derives every candidate's permission
-- and scope from the tables (never from the caller), so a customer cannot
-- name a recipient, and a staff member outside the ticket's scope never
-- learns it exists. The payload carries the complaint id so the console can
-- open that complaint's chat straight from the notification.
--
-- Part 2 (revisions): cheap change signatures for the staff customer and
-- user directories. The console polls these tiny aggregates instead of
-- re-downloading every row, and only refetches the directory when the
-- signature actually moved. Every signal is either a small-table aggregate
-- or an index-backed max(created_at) on the partitioned complaints table.

-- Parameterised twin of staff_scope_matches(): evaluates an arbitrary staff
-- user's company/insurance-type/ownership scope against one complaint. Used
-- by the notice fan-out, which must judge candidates other than the caller.
CREATE OR REPLACE FUNCTION app_security.staff_scope_covers(
    p_user_id uuid, p_company_id uuid, p_insurance_type insurance_type,
    p_agent_id uuid, p_team_id uuid
) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT EXISTS (SELECT 1 FROM public.staff_users
                   WHERE id = p_user_id AND status = 'active')
        AND EXISTS (SELECT 1 FROM public.user_company_scope s
                    WHERE s.user_id = p_user_id
                      AND (s.all_companies OR s.company_id = p_company_id))
        AND (NOT EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                         WHERE t.user_id = p_user_id)
             OR EXISTS (SELECT 1 FROM public.user_insurance_type_scope t
                        WHERE t.user_id = p_user_id
                          AND t.insurance_type = p_insurance_type))
        AND EXISTS (SELECT 1 FROM public.user_ownership_level o
                    WHERE o.user_id = p_user_id AND (
                        o.level = 'all'
                        OR (o.level = 'own' AND p_agent_id = p_user_id)
                        OR (o.level = 'team' AND EXISTS (
                            SELECT 1 FROM public.team_members tm
                            WHERE tm.team_id = p_team_id AND tm.user_id = p_user_id))))
$function$;
REVOKE ALL ON FUNCTION app_security.staff_scope_covers(uuid, uuid, insurance_type, uuid, uuid)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.staff_scope_covers(uuid, uuid, insurance_type, uuid, uuid)
    TO app_runtime;

-- Customer-initiated fan-out to scoped staff. Returns the number notified.
CREATE FUNCTION app_security.queue_staff_notice(
    p_id uuid, p_created timestamptz, p_kind text
) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE
    v_ticket complaints%ROWTYPE;
    v_notified integer := 0;
BEGIN
    -- Only a customer action may fan out to staff, and only for the two
    -- customer-visible kinds; staff-side events keep their own paths.
    IF app_security.actor_scope() <> 'customer'
       OR p_kind NOT IN ('created', 'reply')
       OR NOT app_security.can_read_complaint_id(p_id, p_created) THEN
        RETURN 0;
    END IF;
    SELECT * INTO v_ticket FROM public.complaints c
    WHERE c.id = p_id AND c.created_at = p_created;
    IF v_ticket.id IS NULL THEN RETURN 0; END IF;
    INSERT INTO public.notifications
        (recipient_staff_id, complaint_id, complaint_created_at, kind, payload)
    SELECT s.id, p_id, p_created, p_kind,
        jsonb_build_object('complaint_id', p_id, 'ticket_no', v_ticket.ticket_no,
            'status', v_ticket.status::text, 'kind', p_kind)
    FROM public.staff_users s
    WHERE s.status = 'active'
      -- Deny-wins permission resolution, identical to has_permission().
      AND NOT EXISTS (SELECT 1 FROM public.user_permission_overrides o
                      WHERE o.user_id = s.id AND o.permission_code = 'complaints.view'
                        AND o.effect = 'deny')
      AND (EXISTS (SELECT 1 FROM public.user_permission_overrides o
                   WHERE o.user_id = s.id AND o.permission_code = 'complaints.view'
                     AND o.effect = 'allow')
           OR EXISTS (SELECT 1 FROM public.user_roles ur
                      JOIN public.role_permissions rp ON rp.role_id = ur.role_id
                      WHERE ur.user_id = s.id AND rp.permission_code = 'complaints.view'))
      AND app_security.staff_scope_covers(
              s.id, v_ticket.company_id, v_ticket.insurance_type,
              v_ticket.assigned_agent_id, v_ticket.team_id)
    -- The assignee is never crowded out; the remainder is bounded so a burst
    -- of customer messages cannot grow an unbounded fan-out.
    ORDER BY (s.id = v_ticket.assigned_agent_id) DESC, s.id
    LIMIT 100;
    GET DIAGNOSTICS v_notified = ROW_COUNT;
    RETURN v_notified;
END;
$function$;
REVOKE ALL ON FUNCTION app_security.queue_staff_notice(uuid, timestamptz, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.queue_staff_notice(uuid, timestamptz, text) TO app_runtime;

-- Change signature for the staff customer directory. Same gate as
-- customer_directory(): users.view with a global scope. Returns '' when the
-- caller may not read the directory at all.
CREATE FUNCTION app_security.customer_directory_revision()
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.is_active_staff() AND app_security.has_permission('users.view')
            AND app_security.has_global_scope() THEN concat_ws('|',
                (SELECT count(*) FROM public.customers)::text,
                to_char((SELECT max(updated_at) FROM public.customers), 'YYYYMMDDHH24MISSUS'),
                (SELECT count(*) FROM public.policies)::text,
                to_char((SELECT max(created_at) FROM public.complaints), 'YYYYMMDDHH24MISSUS'))
            ELSE '' END
$function$;
REVOKE ALL ON FUNCTION app_security.customer_directory_revision() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.customer_directory_revision() TO app_runtime;

-- Change signature for the staff user directory. Same gate as
-- staff_directory(): users.view with a global scope.
CREATE FUNCTION app_security.staff_directory_revision()
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
    SELECT CASE WHEN app_security.is_active_staff() AND app_security.has_permission('users.view')
            AND app_security.has_global_scope() THEN concat_ws('|',
                (SELECT count(*) FROM public.staff_users)::text,
                to_char((SELECT max(updated_at) FROM public.staff_users), 'YYYYMMDDHH24MISSUS'),
                (SELECT count(*) FROM public.departments)::text,
                (SELECT count(*) FROM public.user_roles)::text)
            ELSE '' END
$function$;
REVOKE ALL ON FUNCTION app_security.staff_directory_revision() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.staff_directory_revision() TO app_runtime;
