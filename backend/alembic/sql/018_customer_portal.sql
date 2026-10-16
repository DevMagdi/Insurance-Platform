-- Phase 6 customer portal: notification preferences, self-service profile,
-- assigned-agent display and customer notification read receipts.
-- All customer writes go through SECURITY DEFINER functions scoped to the
-- authenticated customer actor; app_runtime never gains blanket UPDATE on
-- the customers or notifications tables.

ALTER TABLE public.customers
    ADD COLUMN notification_preferences jsonb NOT NULL
    DEFAULT '{"email": true, "whatsapp": true, "sms": true}'::jsonb;

ALTER TABLE public.customers
    ADD CONSTRAINT ck_customer_notification_preferences
    CHECK (jsonb_typeof(notification_preferences) = 'object');

GRANT SELECT (notification_preferences) ON public.customers TO app_runtime;

CREATE FUNCTION app_security.customer_profile() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_result jsonb;
BEGIN
    IF app_security.actor_scope() <> 'customer' THEN
        RETURN NULL;
    END IF;
    SELECT jsonb_build_object(
        'id', c.id,
        'full_name', c.full_name,
        'mobile', c.mobile,
        'email', c.email,
        'whatsapp_available', c.whatsapp_available,
        'national_id_masked', CASE WHEN c.national_id_last4 IS NULL
            THEN '**************'
            ELSE '**********' || c.national_id_last4 END,
        'notification_preferences', COALESCE(c.notification_preferences,
            '{"email": true, "whatsapp": true, "sms": true}'::jsonb)
    ) INTO v_result
    FROM public.customers c
    WHERE c.id = app_security.actor_id();
    RETURN v_result;
END;
$function$;

CREATE FUNCTION app_security.update_customer_profile(
    p_full_name text, p_email text, p_whatsapp boolean, p_preferences jsonb
) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_email text := nullif(lower(btrim(coalesce(p_email, ''))), '');
BEGIN
    IF app_security.actor_scope() <> 'customer'
       OR length(btrim(coalesce(p_full_name, ''))) NOT BETWEEN 2 AND 120
       OR (v_email IS NOT NULL AND (length(v_email) > 254
           OR v_email !~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'))
       OR p_preferences IS NULL
       OR jsonb_typeof(p_preferences) <> 'object'
       OR EXISTS (
           SELECT 1 FROM jsonb_each(p_preferences) AS entry(key, value)
           WHERE entry.key NOT IN ('email', 'whatsapp', 'sms')
              OR jsonb_typeof(entry.value) <> 'boolean'
       ) THEN
        RAISE EXCEPTION 'invalid customer profile' USING ERRCODE = '22023';
    END IF;
    UPDATE public.customers SET
        full_name = btrim(p_full_name),
        email = v_email,
        whatsapp_available = COALESCE(p_whatsapp, false),
        notification_preferences = (
            '{"email": true, "whatsapp": true, "sms": true}'::jsonb || p_preferences
        ),
        updated_at = now()
    WHERE id = app_security.actor_id();
    IF NOT FOUND THEN
        RAISE EXCEPTION 'customer unavailable' USING ERRCODE = '42501';
    END IF;
    RETURN app_security.customer_profile();
END;
$function$;

-- Only a display name is exposed to actors who can already read the complaint;
-- no staff email, role or other staff PII leaves through this helper.
CREATE FUNCTION app_security.assigned_agent_display(
    p_id uuid, p_created timestamptz
) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
DECLARE v_name text;
BEGIN
    IF NOT app_security.can_read_complaint_id(p_id, p_created) THEN
        RETURN NULL;
    END IF;
    SELECT s.full_name INTO v_name
    FROM public.complaints c
    JOIN public.staff_users s ON s.id = c.assigned_agent_id
    WHERE c.id = p_id AND c.created_at = p_created;
    RETURN v_name;
END;
$function$;

CREATE FUNCTION app_security.mark_customer_notification_read(p_id uuid) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $function$
BEGIN
    IF app_security.actor_scope() <> 'customer' THEN
        RETURN false;
    END IF;
    UPDATE public.notifications
    SET read_at = COALESCE(read_at, now())
    WHERE id = p_id AND recipient_customer_id = app_security.actor_id();
    RETURN FOUND;
END;
$function$;

REVOKE ALL ON FUNCTION app_security.customer_profile() FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.update_customer_profile(text, text, boolean, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.assigned_agent_display(uuid, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION app_security.mark_customer_notification_read(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app_security.customer_profile() TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.update_customer_profile(text, text, boolean, jsonb) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.assigned_agent_display(uuid, timestamptz) TO app_runtime;
GRANT EXECUTE ON FUNCTION app_security.mark_customer_notification_read(uuid) TO app_runtime;
