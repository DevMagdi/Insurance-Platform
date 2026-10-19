# Live staff updates — manual verification

1. Sign in as a customer, open a ticket, and post a reply.
2. As staff (with `complaints.view`), open the same ticket in the staff
   console and keep it open: the reply appears within a few seconds
   without a page refresh (`GET /api/staff/complaints/{id}/stream`, SSE;
   a 15s quiet poll covers SSE outages).
3. The staff complaints queue updates live over the existing
   `/api/staff/complaints/stream` WebSocket (fresh scoped snapshot on
   connect/refresh/heartbeat, 20s fallback poll).
4. Admin dashboard widgets and the customer directory refresh every 30s
   while the page is visible.

Every live channel re-validates the session, token version, permissions
and AccessScope on each poll/snapshot and only ever emits status +
signature metadata — never message bodies. Scope changes therefore stop
updates on the next tick instead of leaking a stale subscription.
