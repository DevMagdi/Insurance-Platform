"""Locust load test: complaint submission, tracking and scoped dashboards.

    python -m app.scripts.loadtest_sessions --customers 500 --staff 50 > sessions.json
    locust -f loadtest/locust/locustfile.py --host http://127.0.0.1:8000 \
        --users 300 --spawn-rate 30 --run-time 5m --headless --csv results/run

Run the API with ``RATE_LIMIT_MULTIPLIER`` raised (non-production only) when the
goal is capacity rather than abuse controls: a single load generator IP would
otherwise be throttled by the per-IP sliding windows, which is correct behaviour.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from locust import HttpUser, between, task

with open(os.environ.get("SESSIONS", "sessions.json"), encoding="utf-8") as handle:
    FIXTURES: dict[str, list[dict[str, str]]] = json.load(handle)

_customers = itertools.cycle(FIXTURES["customers"])
_tracking = itertools.cycle(FIXTURES["tracking"])
_staff = itertools.cycle(FIXTURES["staff"])
CSRF = "locust-csrf"
_today = datetime.now(UTC).date()
# Dashboards always query a bounded window (the API rejects open-ended ranges).
WINDOW = (
    f"date_from={(_today - timedelta(days=30)).isoformat()}T00:00:00Z"
    f"&date_to={_today.isoformat()}T23:59:59Z"
)


def _accept(response: Any, *codes: int) -> None:
    if response.status_code in codes:
        response.success()
    elif response.status_code == 429:
        response.failure("throttled (429)")
    else:
        response.failure(f"unexpected {response.status_code}")


class Customer(HttpUser):
    weight = 3
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.me = next(_customers)
        self.client.cookies.set("access_token", self.me["access"])
        self.client.cookies.set("csrf_token", CSRF)

    @task(1)
    def submit_complaint(self) -> None:
        body = {
            "national_id": self.me["national_id"],
            "mobile": self.me["mobile"],
            "company_id": self.me["company_id"],
            "insurance_type": self.me["insurance_type"],
            "policy_number": self.me["policy_number"],
            "subject": "Locust: delayed claim payment",
            "description": "Synthetic load-test complaint describing a delayed reimbursement.",
        }
        headers = {
            "X-CSRF-Token": CSRF,
            "Idempotency-Key": f"locust-{uuid.uuid4().hex}",
        }
        with self.client.post(
            "/api/customer/complaints",
            json=body,
            headers=headers,
            name="POST /api/customer/complaints",
            catch_response=True,
        ) as response:
            _accept(response, 200, 201)

    @task(4)
    def my_tickets(self) -> None:
        with self.client.get(
            "/api/customer/complaints?page=1&page_size=10",
            name="GET /api/customer/complaints",
            catch_response=True,
        ) as response:
            _accept(response, 200)
        with self.client.get(
            "/api/customer/complaints/counters",
            name="GET /api/customer/complaints/counters",
            catch_response=True,
        ) as response:
            _accept(response, 200)


class GuestTracker(HttpUser):
    weight = 4
    wait_time = between(0.5, 2)

    @task
    def track(self) -> None:
        fixture = next(_tracking)
        with self.client.post(
            "/api/guest/track",
            json={
                "ticket_no": fixture["ticket_no"],
                "national_id": fixture["national_id"],
            },
            name="POST /api/guest/track",
            catch_response=True,
        ) as response:
            _accept(response, 200)


class StaffDashboard(HttpUser):
    weight = 1
    wait_time = between(2, 5)

    def on_start(self) -> None:
        self.me = next(_staff)
        self.client.cookies.set("access_token", self.me["access"])
        self.client.cookies.set("csrf_token", CSRF)

    @task(3)
    def analytics(self) -> None:
        path = random.choice(["overview", "rollup", "scorecards"])
        with self.client.get(
            f"/api/staff/analytics/{path}?{WINDOW}",
            name=f"GET /api/staff/analytics/{path}",
            catch_response=True,
        ) as response:
            _accept(response, 200, 403)

    @task(2)
    def complaint_queue(self) -> None:
        with self.client.get(
            "/api/staff/complaints?page=1&page_size=25",
            name="GET /api/staff/complaints",
            catch_response=True,
        ) as response:
            _accept(response, 200, 403)
