// k6 load test for the three critical journeys:
//   1. complaint submission (authenticated customers, idempotent POST)
//   2. tracking (guest track + customer list/detail)
//   3. scoped dashboard analytics (staff, RLS + scope-aware cache)
//
// Fixtures come from `python -m app.scripts.loadtest_sessions` (non-production).
//   k6 run -e BASE_URL=http://127.0.0.1:8000 -e SESSIONS=/tmp/sessions.json \
//          -e PROFILE=smoke loadtest/k6/platform.js
// PROFILE: smoke (CI gate) | load (sustained) | stress (find the knee) | spike.
import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter, Rate } from 'k6/metrics';
import exec from 'k6/execution';

const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const PROFILE = __ENV.PROFILE || 'smoke';
const data = new SharedArray('sessions', () => [JSON.parse(open(__ENV.SESSIONS || './sessions.json'))]);
const fixtures = data[0];

const unexpected = new Rate('unexpected_status');
const throttled = new Counter('throttled_429');

const PROFILES = {
  smoke: { submit: [5, '30s'], track: [5, '30s'], dashboard: [3, '30s'] },
  load: { submit: [100, '10m'], track: [300, '10m'], dashboard: [60, '10m'] },
  stress: { submit: [400, '15m'], track: [1200, '15m'], dashboard: [200, '15m'] },
  spike: { submit: [600, '3m'], track: [2000, '3m'], dashboard: [300, '3m'] },
};
const p = PROFILES[PROFILE];
// Dashboards always query a bounded window (the API rejects open-ended ranges).
const today = new Date();
const monthAgo = new Date(today.getTime() - 30 * 86400000);
const day = (d) => d.toISOString().slice(0, 10);
const WINDOW = `date_from=${day(monthAgo)}T00:00:00Z&date_to=${day(today)}T23:59:59Z`;

function ramp([target, duration]) {
  return {
    executor: 'ramping-vus',
    startVUs: 0,
    stages: [
      { duration: PROFILE === 'smoke' ? '5s' : '2m', target },
      { duration, target },
      { duration: PROFILE === 'smoke' ? '5s' : '1m', target: 0 },
    ],
    gracefulRampDown: '10s',
  };
}

export const options = {
  scenarios: {
    complaint_submission: { ...ramp(p.submit), exec: 'submitComplaint' },
    tracking: { ...ramp(p.track), exec: 'tracking' },
    scoped_dashboard: { ...ramp(p.dashboard), exec: 'dashboard' },
  },
  thresholds: {
    'http_req_failed{expected_response:true}': ['rate<0.01'],
    unexpected_status: ['rate<0.01'],
    'http_req_duration{scenario:complaint_submission}': ['p(95)<800'],
    'http_req_duration{scenario:tracking}': ['p(95)<400'],
    'http_req_duration{scenario:scoped_dashboard}': ['p(95)<1200'],
  },
  summaryTrendStats: ['avg', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function authHeaders(access) {
  return {
    cookies: { access_token: access, csrf_token: 'k6-csrf' },
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': 'k6-csrf' },
  };
}

function record(res, ok) {
  if (res.status === 429) throttled.add(1);
  unexpected.add(!ok && res.status !== 429);
}

function pick(list) {
  return list[exec.scenario.iterationInTest % list.length];
}

export function submitComplaint() {
  const c = pick(fixtures.customers);
  const params = authHeaders(c.access);
  params.headers['Idempotency-Key'] = `k6-${exec.vu.idInTest}-${exec.scenario.iterationInTest}-${Date.now()}`;
  params.tags = { name: 'POST /api/customer/complaints' };
  const body = JSON.stringify({
    national_id: c.national_id,
    mobile: c.mobile,
    company_id: c.company_id,
    insurance_type: c.insurance_type,
    policy_number: c.policy_number,
    subject: 'Load test: delayed claim payment',
    description: 'Synthetic load-test complaint describing a delayed reimbursement for a procedure.',
  });
  const res = http.post(`${BASE}/api/customer/complaints`, body, params);
  record(res, check(res, { 'submit 201/200': (r) => r.status === 201 || r.status === 200 }));
  sleep(1 + Math.random());
}

export function tracking() {
  group('guest track', () => {
    const t = pick(fixtures.tracking);
    const res = http.post(
      `${BASE}/api/guest/track`,
      JSON.stringify({ ticket_no: t.ticket_no, national_id: t.national_id }),
      { headers: { 'Content-Type': 'application/json' }, tags: { name: 'POST /api/guest/track' } },
    );
    record(res, check(res, { 'track 200': (r) => r.status === 200 }));
  });
  group('customer list + counters', () => {
    const c = pick(fixtures.customers);
    const params = { ...authHeaders(c.access), tags: { name: 'GET /api/customer/complaints' } };
    const list = http.get(`${BASE}/api/customer/complaints?page=1&page_size=10`, params);
    record(list, check(list, { 'list 200': (r) => r.status === 200 }));
    const counters = http.get(`${BASE}/api/customer/complaints/counters`, {
      ...authHeaders(c.access),
      tags: { name: 'GET /api/customer/complaints/counters' },
    });
    record(counters, check(counters, { 'counters 200': (r) => r.status === 200 }));
  });
  sleep(0.5 + Math.random());
}

export function dashboard() {
  const s = pick(fixtures.staff);
  for (const path of ['overview', 'rollup', 'scorecards']) {
    const res = http.get(`${BASE}/api/staff/analytics/${path}?${WINDOW}`, {
      ...authHeaders(s.access),
      tags: { name: `GET /api/staff/analytics/${path}` },
    });
    // Staff without analytics permission must get 403 (scope enforcement is part of the test).
    record(res, check(res, { 'analytics 200/403': (r) => r.status === 200 || r.status === 403 }));
  }
  const list = http.get(`${BASE}/api/staff/complaints?page=1&page_size=25`, {
    ...authHeaders(s.access),
    tags: { name: 'GET /api/staff/complaints' },
  });
  record(list, check(list, { 'staff list 200/403': (r) => r.status === 200 || r.status === 403 }));
  sleep(2 + Math.random() * 2);
}
