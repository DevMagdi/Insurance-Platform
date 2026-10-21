# PROJECT BRIEF – Insurance Brokerage Request Management System

## 1. Business description

A request (طلبات) system for an insurance brokerage company. Policyholders of different insurance companies come to the brokerage with requests — approvals, reimbursements, card and policy questions, and complaints among them. The brokerage staff receive, assign, follow up, and resolve them, and can forward them to the insurance company. The product must look premium and VIP, modern and updated, to attract clients and make them want to buy it.

Requests arrive on three channels, and WhatsApp is the busiest of them. They almost always carry attachments, and a request raised on WhatsApp must appear in the platform at once. Most requests come from employees of companies contracted with the brokerage, so the platform also answers to those companies directly.

## 2. Customer side (portal)

2.1 Submit a request via a form with these fields:

- Full name

- National ID (Egyptian, 14 digits, validated)

- Contact mobile number + a toggle "available on WhatsApp"

- Insurance company (dropdown managed by admin)

- Insurance type: Health / General / Both

- Insurance policy number

- Complaint (subject + details)

- Attachments (upload multiple files)

After submission the customer gets a unique ticket number and a confirmation.

2.2 Track the request at any time:

- See the request status change over time: New → Assigned (someone is assigned to it) → In Review → Resolved (plus Reopened and Transferred/Escalated to the insurance company).

- See a dashboard with all their tickets: open, closed, in review, transferred, and total ticket counts.

- Access any ticket they ever opened, at any time, with its full timeline, replies, and attachments.

- Reply, add attachments, reopen after resolution, rate satisfaction.

## 2b. Intake channels

A request reaches the platform through one of three doors, and lands in the
same queue whichever door it used.

- **WhatsApp** — the primary channel, and almost always carries attachments.
  An incoming message opens a request immediately; it does not wait for a
  ticket number to be quoted.
- **Phone calls** — an inbound call becomes a request in its own right. Call
  metadata (direction, outcome, duration) is always kept. A recording is
  stored only when the caller consented, encrypted at rest, and the
  transcript is encrypted separately; neither is ever exposed to a corporate
  reader, who sees only that the call happened and how long it ran.
- **Email** — rare, and also ticket-optional.

A request needs an insurance number **or** a card number, the customer's
mobile number, and the customer's name. When neither reference arrives, the
request is still opened and flagged as awaiting one rather than rejected.

Messages from the same mobile continue an existing open request for 72 hours
instead of opening a duplicate; a quoted ticket number always wins over that
rule.

## 2c. Corporate client portal

The second interface. A company contracted with the brokerage signs in with
its own username and password and sees only its own data:

- its policies and documents, downloadable;
- its employees' requests, searchable by employee name and mobile number,
  grouped under the employee who raised them;
- the **archive**: every request the company ever raised, open or closed, in
  one searchable place, each opening into its messages, attachments, calls
  and status history. Staff notes marked internal are never shown;
- the contract value with the brokerage, the payment frequency (monthly,
  quarterly, semi-annual, annual), and the renewal date with a countdown.

## 2d. Automation

Work the platform does without being asked, each action recorded once in an
automation ledger so a retry can never double-send:

- a new request is acknowledged to the customer automatically, within a
  24-hour window after it arrives;
- a contract renewal is flagged ahead of its date, keyed on the date itself
  so moving the renewal re-arms the reminder;
- a request with no customer reply for a quiet period is closed
  automatically. An internal staff note does not count as having answered
  the customer.

## 3. Brokerage side (dashboard)

3.1 Statistics: how many requests arrived within a date range the user chooses; graphs and analysis.

3.2 Filters (combinable): request status (New, Assigned, In Review, Resolved), date range, insurance company, insurance type (Health, General, Both), intake source (WhatsApp, phone, email, portal), and customer mobile number. The user can see the status of any request at any time.

3.3 Other useful data and reports (resolution times, SLA, per agent, per company, recurring issues, CSAT, exports).

3.4 Integrations:

- Email: connected at any time; actions on a request such as "send this request by email" (e.g. to the insurance company). Inbound mail opens a request even when the sender does not quote a ticket number.

- WhatsApp: connected, two-way conversation and sending actions.

- AI Voice (later phase, architecture now): an AI voice agent that answers/calls customers.

3.5 Additional features proposed (must be included): SLA engine and escalation, auto-assignment, AI classification/sentiment/summary/suggested replies, duplicate detection, real-time notifications, unified inbox for all channels, CSAT surveys, scheduled email reports, company scorecards, full-text search, audit log, PWA/mobile-first, public landing page with CTA.

## 4. Staff access management (controlled by Admin)

- The Admin dashboard controls all other staff users.

- Granular permissions per feature (resource.action); custom roles built from permissions; per-user Allow/Deny overrides.

- Data scope per user: specific insurance companies or all companies; optionally insurance type (Health/General/Both) and ownership level (own / team / all).

- Enforced on the server for every query (tickets, analytics, search, exports, notifications, websockets, attachments, emails) + PostgreSQL Row-Level Security.

- Anti-escalation rules, access-change audit log, live "effective access preview", instant revocation.

## 5. Design

Modern, updated, premium/VIP: deep navy + gold/emerald accents, glassmorphism, soft gradients, Arabic-first RTL + English, dark/light mode, smooth micro-animations, polished charts/tables, fully responsive, WCAG AA. It should feel like a high-end SaaS product that sells itself.

## 6. Technical stack

- Frontend: React JS + Vite + Tailwind CSS (+ TanStack Query, React Hook Form + Zod, Recharts, Framer Motion, i18n).

- Backend: FastAPI (async) + PostgreSQL (SQLAlchemy 2 + Alembic) + Redis + Celery/ARQ. Private binaries (attachments, export artifacts, call recordings) are stored AES-256-GCM-encrypted in PostgreSQL columns under `APP_ENCRYPTION_KEY`; there is no S3/MinIO/SeaweedFS dependency and no second stateful service.

- Infra: Docker Compose (dev), Nginx, Kubernetes-ready.

## 7. Non-functional requirements

- Scalable to 1,000,000 concurrent users: stateless API, horizontal scaling, PgBouncer, read replicas, partitioning, caching, queues.

- Security: OWASP Top 10, JWT with rotating refresh tokens, OTP for customers, 2FA for staff, PII encryption (national ID), signed attachment URLs, audit logs, Egypt PDPL (Law 151/2020).

- Rate limiting and abuse protection on all public endpoints (login, OTP, request submission, uploads, exports).

- Observability: structured logs, metrics, Sentry, health checks.

- Quality: clean typed modular code, tests, docs, CI/CD.
