-- The voice follow-up scheduler reads the customer's consent flags directly
-- (`select customers.mobile, voice_followup_opt_in, voice_consent_at`), but the
-- runtime role was only ever granted the original customer columns. Every call
-- to `POST /api/staff/complaints/{id}/voice-followups` therefore died with
-- `permission denied for table customers` (HTTP 500) before the consent check
-- could even run. Grant the two consent columns added in revision 017.
GRANT SELECT (voice_followup_opt_in, voice_consent_at) ON public.customers TO app_runtime;
