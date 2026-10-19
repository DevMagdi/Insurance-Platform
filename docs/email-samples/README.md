# Transactional email branding

`insurer-forward.html` is an illustrative, non-customer-data rendering of the actual
shared email layout. Its example origin/contact are placeholders, not live services.

Deployment configuration (API **and worker**, including external worker deployments):

- `PUBLIC_APP_URL`: the public application origin, HTTPS in production. Emails use
  `/api/public/brand/logo` and the portal at that origin. Upload the platform logo
  in the existing branding settings. The platform name is always visible, including
  when images are blocked or no uploaded logo is available.
- `SUPPORT_EMAIL`: the support address shown in both alternatives of every message.
  Replace the deliberately non-delivering default `support@example.invalid` before
  production use. It is independent of `SMTP_FROM` (which can be a no-reply sender).
- The existing database `platform_name` loader supplies the current platform name
  to SMTP, staff account mail and queued integration mail.

SMTP, SES, Gmail and Outlook deliver MIME alternatives; Outlook uses Graph's raw
base64 MIME interface. SendGrid receives both content alternatives. Report
attachments remain outside the nested multipart/alternative body. Text templates
retain their two-value interface; `branded_email` is the shared final rendering
boundary so persisted outbox jobs also receive current branding at delivery time.

All dynamic content is escaped. Insurer forwards include only the approved
customer-visible description and explicitly supplied forward note, not private
staff-note records; the existing identifier minimizer is applied before queuing.
The legacy escalation notice has no policy/type data and labels these as not
provided rather than inventing values.

Verification:

- `make test`: template locale/escaping/footer tests, mocked SMTP MIME inspection
  for OTP, invitation, reset, received/status notices, insurer and reports; provider
  contract tests and staff workplace-delivery assertions.
- `backend/tests/test_rls_integration.py::test_complaint_policy_link_and_directory`:
  real complaint creation with runtime RLS, policy reuse/new-number behavior,
  and historical complaint company inclusion in the guarded directory. Uses the
  suite's disposable `TEST_*` database configuration.
- `cd frontend && npm run test -- CustomersPage`: includes a customer associated
  through a historical complaint without any policy in the response.
