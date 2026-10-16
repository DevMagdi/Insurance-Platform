# PROJECT BRIEF – Insurance Brokerage Complaint Management System

## 1. Business description

A complaint system for an insurance brokerage company. Policyholders of different insurance companies come to the brokerage to file complaints. The brokerage staff receive, assign, follow up, and resolve them, and can forward them to the insurance company. The product must look premium and VIP, modern and updated, to attract clients and make them want to buy it.

## 2. Customer side (portal)

2.1 Submit a complaint via a form with these fields:

- Full name

- National ID (Egyptian, 14 digits, validated)

- Contact mobile number + a toggle "available on WhatsApp"

- Insurance company (dropdown managed by admin)

- Insurance type: Health / General / Both

- Insurance policy number

- Complaint (subject + details)

- Attachments (upload multiple files)

After submission the customer gets a unique ticket number and a confirmation.

2.2 Track the complaint at any time:

- See the complaint status change over time: New → Assigned (someone is assigned to it) → In Review → Resolved (plus Reopened and Transferred/Escalated to the insurance company).

- See a dashboard with all their tickets: open, closed, in review, transferred, and total ticket counts.

- Access any ticket they ever opened, at any time, with its full timeline, replies, and attachments.

- Reply, add attachments, reopen after resolution, rate satisfaction.

## 3. Brokerage side (dashboard)

3.1 Statistics: how many complaints arrived within a date range the user chooses; graphs and analysis.

3.2 Filters (combinable): complaint status (New, Assigned, In Review, Resolved), date range, insurance company, insurance type (Health, General, Both). The user can see the status of any complaint at any time.

3.3 Other useful data and reports (resolution times, SLA, per agent, per company, recurring issues, CSAT, exports).

3.4 Integrations:

- Email: connected at any time; actions on a complaint such as "send this complaint by email" (e.g. to the insurance company).

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

- Backend: FastAPI (async) + PostgreSQL (SQLAlchemy 2 + Alembic) + Redis + Celery/ARQ + S3-compatible storage (SeaweedFS locally; managed S3 recommended for production).

- Infra: Docker Compose (dev), Nginx, Kubernetes-ready.

## 7. Non-functional requirements

- Scalable to 1,000,000 concurrent users: stateless API, horizontal scaling, PgBouncer, read replicas, partitioning, caching, queues.

- Security: OWASP Top 10, JWT with rotating refresh tokens, OTP for customers, 2FA for staff, PII encryption (national ID), signed attachment URLs, audit logs, Egypt PDPL (Law 151/2020).

- Rate limiting and abuse protection on all public endpoints (login, OTP, complaint submission, uploads, exports).

- Observability: structured logs, metrics, Sentry, health checks.

- Quality: clean typed modular code, tests, docs, CI/CD.
