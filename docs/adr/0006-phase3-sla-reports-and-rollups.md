# 0006 — Business-hours SLA, scheduled reports, rollup and provider settings

Status: accepted (Phase 3)

## Context

Phase 2 shipped wall-clock SLAs, opt-in round-robin/least-workload assignment and
a CSV-only async export. Phase 3 had to close four platform gaps without
weakening the two invariants the codebase already enforces everywhere:

1. Every read and every write is bounded by the caller's *current* permission,
   company/type scope and ownership level — in PostgreSQL, not only in Python.
2. Anything the background worker does on behalf of a user must re-derive that
   user's authorization at execution time; a stored decision is not an
   authorization decision.

The four gaps were: calendar-aware SLA rules with alerts; scheduled email
reports; a materialized analytics view; and administration of provider
integrations, assignment rules and duplicate candidates.

## Decisions

### Business-hours SLAs are computed in the database

`add_business_minutes(created_at, minutes, calendar)` walks weekly windows and
holidays in the calendar's IANA zone. `{}` means continuous 24/7 coverage, so
existing rules keep their meaning. `set_ticket_sla` matches the most specific
active rule (company/type/priority down to the global default) and stores both
the response deadline and the resolution deadline plus an alert timestamp.
The response clock is stopped by the first *public* staff reply; internal notes
deliberately do not stop it. `process_sla_warnings` runs before
`process_sla_breaches` so "at risk" precedes "missed" in the timeline.

Calendars are validated twice: pydantic rejects unknown zones, empty or
inverted windows and malformed holidays, and `valid_business_hours` re-checks
the same shape for any other writer.

### Scheduled reports are reauthorized per run, not per schedule

A schedule is owned by one staff user. The worker claim function re-checks that
owner's status, role permissions, deny overrides and scope rows *inside the
claim transaction*; a revoked owner has the schedule deactivated and is
notified instead of receiving data. Row production switches the transaction to
the owner's identity and filters with `can_read_complaint` per row, reviving a
relative window at run time so a schedule created months ago cannot expose
newer tickets. Recipients are delivery addresses, never an authorization
source. Three consecutive failures disable the schedule.

### The analytics rollup is an optimization, never an authority

`analytics_daily` aggregates per UTC day and dimension. Nullable dimensions use
an all-zero UUID sentinel so the unique index remains valid for a future
`CONCURRENTLY` refresh. Runtime reads go through `analytics_daily_scoped`, which
re-applies company scope, insurance-type scope and a fail-closed ownership
predicate per row. Because a day-granular view cannot answer partial days or
free-text search, the API uses it only for whole-UTC-day windows without search
and only while the view is fresh; otherwise the response is served from the
ordinary scoped query and reports `source: "live"`.

### Integration settings store references, not credentials

Config keys are whitelisted `lower_snake_case` values of limited length, and key
names that look like credentials (`secret`, `token`, `password`, `api_key`,
`private`, `credential`, `key`) are rejected outright. The secret itself stays in
a vault; `secret_ref` points at it and is required before an integration may be
enabled. Provider adapters are intentionally small: SMTP delivery is real, every
other adapter fails closed until it is implemented.

### One renderer for every artifact

CSV, Excel (minimal OOXML) and PDF share a single renderer that only receives
already-scoped rows, escapes spreadsheet formulas through the existing
CSV-injection guard and enforces the 1,000-row / 2 MB budget. Excel numbers stay
numeric so a report can be pivoted.

## Consequences

- Deadline math lives in one place and is testable without a Python scheduler;
  the Python side only computes the *next cron time* for user feedback and the
  worker hand-off.
- A user whose access was reduced cannot keep receiving scheduled data or
  previously exported rows; the cost is an extra authorization query per run.
- Rollup numbers are exact for whole days and never silently partial: an
  ineligible window always falls back to the live query.
- Adding a provider means implementing an adapter and a vault entry, not
  relaxing the settings guard.
- The forward-only SQL revisions (012–016) cannot be rolled back in place;
  recovery is restore-from-backup, as with earlier revisions.
