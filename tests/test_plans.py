"""R05 통합 검증. R05_ROLLBACK_TEST=1을 설정한 기존 changwon_dev에서 실행합니다."""

import asyncio
import json
import os
import unittest
from datetime import datetime, time, timedelta
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from config.database import get_connection, get_write_connection
from config.settings import get_settings
from main import app
from service.plan_update_service import record_plan_event


SEOUL = ZoneInfo("Asia/Seoul")


@unittest.skipUnless(os.getenv("R05_ROLLBACK_TEST") == "1", "R05 롤백 통합 검증은 명시적으로 실행합니다.")
class PlanDatabaseTests(unittest.TestCase):
    def test_weekly_and_today_plans_and_contact_flow(self):
        settings = get_settings()
        self.assertTrue(settings.database_url, "기존 API의 DATABASE_URL이 필요합니다.")
        self.org, self.district, self.dong, self.worker, self.other_worker = [uuid4() for _ in range(5)]
        self.plan_ids = []
        self.client_ids = []
        self.batch_ids = []
        with self.connect() as connection:
            database = connection.execute("SELECT current_database() AS name").fetchone()["name"]
            self.assertEqual(database, "changwon_dev")
            self.connection = connection
            self.addCleanup(self.verify_rollback)
            with connection.transaction(force_rollback=True):
                connection.execute("SET LOCAL lock_timeout = '2s'")
                connection.execute("SET LOCAL statement_timeout = '5s'")
                self.now = connection.execute("SELECT transaction_timestamp() AS now").fetchone()["now"]
                self.today = self.now.astimezone(SEOUL).date()
                self.midnight = datetime.combine(self.today, time.min, tzinfo=SEOUL)
                self.week = self.today - timedelta(days=self.today.weekday())
                self.create_scope()
                old_batch = self.create_batch(1, self.now - timedelta(minutes=2))
                batch = self.create_batch(2, self.now - timedelta(minutes=1))
                self.create_batch(3, self.now + timedelta(days=1))
                primary = self.create_plan(batch, 2, "PHONE", with_coordinates=True)
                other_today = self.create_plan(batch, 5, "VISIT", scheduled_at=self.midnight)
                self.create_plan(batch, 1, "VISIT", scheduled_at=self.midnight, worker=self.other_worker)
                next_day = self.midnight + timedelta(days=1)
                self.create_plan(batch, 4, "VISIT", scheduled_at=next_day)
                self.create_plan(batch, 6, "PHONE", scheduled_at=self.midnight)
                self.create_plan(old_batch, 1, "MONITOR")
                auth_settings = settings.model_copy(update={
                    "dev_user_id": self.worker, "contact_write_dev_user_id": self.worker,
                })
                with (
                    patch("util.authentication.get_settings", return_value=auth_settings),
                    patch.dict(app.dependency_overrides, {
                        get_connection: self.reuse_connection,
                        get_write_connection: self.reuse_connection,
                    }),
                ):
                    self.check_queries(batch, primary, other_today)
                    rescheduled = self.check_reviews(primary)
                    self.check_contacts(primary, rescheduled["version"])

    @staticmethod
    def connect():
        return psycopg.connect(
            get_settings().database_url, autocommit=True, row_factory=dict_row, connect_timeout=5,
        )

    def reuse_connection(self):
        yield self.connection

    def create_scope(self):
        self.connection.execute(
            "INSERT INTO organizations (id, name) VALUES (%s, 'R05 롤백 검증 기관')", (self.org,),
        )
        valid_from = self.today - timedelta(days=7)
        self.connection.execute(
            """
            INSERT INTO regions (id, parent_id, name, level, valid_from, reference_date)
            VALUES (%s, NULL, %s, 'DISTRICT', %s, %s), (%s, %s, %s, 'DONG', %s, %s)
            """,
            (self.district, f"R05-{self.district}", valid_from, valid_from,
             self.dong, self.district, f"R05-{self.dong}", valid_from, valid_from),
        )
        self.connection.execute(
            "INSERT INTO organization_regions (organization_id, region_id) VALUES (%s, %s)",
            (self.org, self.district),
        )
        for worker in (self.worker, self.other_worker):
            self.connection.execute(
                """
                INSERT INTO users (id, organization_id, login_id, password_hash, name, role)
                VALUES (%s, %s, %s, '!', 'R05 롤백 검증 담당자', 'WORKER')
                """,
                (worker, self.org, f"R05-{worker}"),
            )
            self.connection.execute(
                "INSERT INTO user_regions (user_id, region_id) VALUES (%s, %s)", (worker, self.district),
            )

    def create_batch(self, generation, as_of_at):
        batch_id = uuid4()
        self.batch_ids.append(batch_id)
        self.connection.execute(
            """
            INSERT INTO plan_batches (id, organization_id, week_start, as_of_at, generation, ranking_policy)
            VALUES (%s, %s, %s, %s, %s, '{"version":"r05-rollback","sort_keys":[],"tie_breaker":"client_id"}')
            """,
            (batch_id, self.org, self.week, as_of_at, generation),
        )
        return batch_id

    def create_plan(self, batch, rank, channel, *, scheduled_at=None, with_coordinates=False, worker=None):
        client, assignment, plan = uuid4(), uuid4(), uuid4()
        self.client_ids.append(client)
        self.plan_ids.append(plan)
        latitude, longitude = (35.0, 128.0) if with_coordinates else (None, None)
        self.connection.execute(
            """
            INSERT INTO clients (id, organization_id, region_id, client_number, name, source_label, latitude, longitude)
            VALUES (%s, %s, %s, %s, 'R05 롤백 검증 대상자', '가상 자료', %s, %s)
            """,
            (client, self.org, self.dong, f"R05-{client}", latitude, longitude),
        )
        self.connection.execute(
            """
            INSERT INTO client_assignments (id, client_id, worker_id, starts_at, reason)
            VALUES (%s, %s, %s, %s, 'R05 롤백 검증 배정')
            """,
            (assignment, client, worker or self.worker, self.now - timedelta(days=3)),
        )
        if worker is not None:
            self.connection.execute(
                """
                INSERT INTO client_assignments (id, client_id, worker_id, starts_at, reason)
                VALUES (%s, %s, %s, %s, 'R05 롤백 검증 복수 배정')
                """,
                (uuid4(), client, self.worker, self.now - timedelta(days=3)),
            )
        row = self.connection.execute(
            """
            INSERT INTO care_plans (
                id, client_id, assignment_id, week_start, batch_id, recommended_channel, channel,
                rank, recommendation_reason, review_status, scheduled_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'R05 롤백 검증 근거', %s, %s)
            RETURNING *
            """,
            (plan, client, assignment, self.week, batch, channel, channel, rank,
             "CONFIRMED" if scheduled_at else "RECOMMENDED", scheduled_at),
        ).fetchone()
        record_plan_event(
            self.connection, plan_before=None, plan_after=row, actor_id=None, request_id=None, action="GENERATED",
        )
        return row

    def request(self, method, path, *, params=None, payload=None, expected=200):
        async def invoke():
            messages = []

            async def receive():
                body = b"" if payload is None else json.dumps(payload).encode()
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await app({
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path, "root_path": "",
                "query_string": urlencode(params or {}).encode(),
                "headers": [(b"content-type", b"application/json")] if payload is not None else [],
                "client": ("127.0.0.1", 12345), "server": ("test", 80),
            }, receive, send)
            status = next(msg["status"] for msg in messages if msg["type"] == "http.response.start")
            body = json.loads(b"".join(msg.get("body", b"") for msg in messages if msg["type"] == "http.response.body"))
            return status, body

        with self.connection.transaction(force_rollback=expected >= 400):
            status, body = asyncio.run(invoke())
            self.assertEqual(status, expected, body)
        if expected >= 400:
            self.assertIsNone(body["data"])
            return body["error"]
        self.assertIsNone(body["error"])
        return body["data"]

    def check_queries(self, batch, primary, other_today):
        params = {"week_start": self.week.isoformat()}
        data = self.request("GET", "/plans", params=params)
        self.assertEqual(data["batch"]["id"], str(batch))
        self.assertEqual(data["total"], 4)
        self.assertEqual([item["rank"] for item in data["items"]], [2, 4, 5, 6])
        self.assertEqual(data["items"][0]["recommendation_reason"], primary["recommendation_reason"])
        self.assertIsNone(data["items"][0]["score"])
        self.assertIsNone(data["items"][0]["last_success_at"])
        filtered = self.request("GET", "/plans", params={
            **params, "region_id": self.dong, "channel": "VISIT", "review_status": "CONFIRMED",
            "execution_status": "NOT_STARTED", "page": 2, "page_size": 1,
        })
        self.assertEqual(filtered["total"], 2)
        self.assertEqual(filtered["items"][0]["plan_id"], str(other_today["id"]))
        self.assertEqual(filtered["items"][0]["rank"], 5)
        empty = self.request("GET", "/plans", params={**params, "region_id": uuid4()})
        self.assertEqual((empty["items"], empty["total"]), ([], 0))
        no_batch = self.request("GET", "/plans", params={"week_start": self.week - timedelta(days=7)})
        self.assertEqual((no_batch["items"], no_batch["total"], no_batch["batch"]), ([], 0, None))
        today = self.request("GET", "/plans/today")
        self.assertEqual(today["today_date"], self.today.isoformat())
        self.assertEqual([item["plan_id"] for item in today["items"]], [str(other_today["id"])])
        self.assertIsNone(today["items"][0]["location"])

    def check_reviews(self, primary):
        path = f"/plans/{primary['id']}/review"
        scheduled_at = self.midnight + timedelta(hours=12)
        confirm = {"request_key": str(uuid4()), "expected_version": 1, "action": "CONFIRM", "channel": "VISIT"}
        self.request("POST", path, payload=confirm, expected=422)
        confirm["scheduled_at"] = scheduled_at.isoformat()
        self.request("POST", path, payload=confirm, expected=422)
        confirm["reason"] = "현장 확인 필요"
        confirmed = self.request("POST", path, payload=confirm)
        self.assertEqual(confirmed["review_status"], "CONFIRMED")
        self.assertEqual(confirmed["execution_status"], "NOT_STARTED")
        self.assertEqual(confirmed["channel_change_reason"], confirm["reason"])
        self.assertEqual(self.request("POST", path, payload=confirm), confirmed)
        conflict = self.request("POST", path, payload={**confirm, "reason": "다른 입력"}, expected=409)
        self.assertEqual(conflict["code"], "REQUEST_KEY_CONFLICT")
        reschedule = {
            "request_key": str(uuid4()), "expected_version": 1, "action": "RESCHEDULE",
            "scheduled_at": (scheduled_at + timedelta(hours=1)).isoformat(),
        }
        self.assertEqual(self.request("POST", path, payload=reschedule, expected=409)["code"], "VERSION_CONFLICT")
        reschedule["expected_version"] = confirmed["version"]
        rescheduled = self.request("POST", path, payload=reschedule)
        visits = self.request("GET", "/plans/today")["items"]
        self.assertEqual([item["order"] for item in visits], [1, 2])
        self.assertEqual(visits[1]["plan_id"], str(primary["id"]))
        self.assertEqual(visits[1]["location"], {"latitude": 35.0, "longitude": 128.0})

        defer = {"request_key": str(uuid4()), "expected_version": rescheduled["version"], "action": "DEFER"}
        self.request("POST", path, payload=defer, expected=422)
        deferred = self.request("POST", path, payload={**defer, "reason": "재방문 조율"})
        self.assertEqual((deferred["review_status"], deferred["scheduled_at"]), ("DEFERRED", None))
        self.assertEqual(len(self.request("GET", "/plans/today")["items"]), 1)
        return self.request("POST", path, payload={
            **confirm, "request_key": str(uuid4()), "expected_version": deferred["version"],
        })

    def check_contacts(self, plan, plan_version):
        client = plan["client_id"]
        form = self.request("GET", f"/clients/{client}/contact-form")
        current_version = form["client_version"]
        path = f"/clients/{client}/contacts"
        answers = []
        for question in form["questions"]:
            if question["is_required"]:
                choices = [choice for choice in question["allowed_answers"] if choice != "NO_RISK"]
                self.assertTrue(choices, "실패와 거부를 검증할 필수 문항 선택지가 필요합니다.")
                answers.append({"question_id": question["question_id"], "answer_code": choices[0]})

        previous_version = plan_version
        visit_at = self.connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        review_path = f"/plans/{plan['id']}/review"
        plan_version = self.request("POST", review_path, payload={
            "request_key": str(uuid4()), "expected_version": plan_version, "action": "RESCHEDULE",
            "channel": "PHONE", "reason": "전화 일정으로 조정",
        })["version"]
        event_versions = []
        last_success = None
        for i, outcome in enumerate(("FAILED", "REFUSED", "SUCCESS", "REFUSED")):
            event_versions.append(previous_version if i == 0 else plan_version)
            occurred_at = visit_at if i == 0 else self.connection.execute(
                "SELECT clock_timestamp() AS now",
            ).fetchone()["now"]
            payload = {
                "request_key": str(uuid4()), "expected_version": current_version,
                "checklist_version_id": form["checklist_version_id"], "answers": answers,
                "channel": "VISIT" if i == 0 else "PHONE", "occurred_at": occurred_at.isoformat(),
                "result": outcome, "next_check_date": occurred_at.astimezone(SEOUL).date().isoformat(),
                "plan_id": str(plan["id"]), "plan_version": plan_version,
            }
            if i == 0:
                mismatch = self.request("POST", path, payload={**payload, "channel": "PHONE"}, expected=422)
                self.assertEqual(mismatch["code"], "INVALID_REQUEST")
                missing_history = self.request(
                    "POST", path, payload={**payload, "occurred_at": self.now.isoformat()}, expected=409,
                )
                self.assertEqual(missing_history["code"], "PLAN_HISTORY_CONFLICT")
                stale = self.request("POST", path, payload={**payload, "plan_version": previous_version}, expected=409)
                self.assertEqual(stale["code"], "PLAN_VERSION_CONFLICT")
            data = self.request("POST", path, payload=payload, expected=201)
            self.assertEqual(data["plan"]["review_status"], "CONFIRMED")
            self.assertEqual(data["plan"]["execution_status"], "CONTACT_FAILED" if i < 2 else "CONFIRMED")
            self.assertEqual(data["plan"]["plan_version"], plan_version + (1 if i in (0, 2) else 0))
            self.assertEqual(self.request("POST", path, payload=payload, expected=201), data)
            if outcome == "SUCCESS":
                last_success = data["last_success_at"]
                self.assertIsNotNone(last_success)
            self.assertEqual(data["last_success_at"], last_success)
            current_version = data["client_version"]
            plan_version = data["plan"]["plan_version"]

        linked = self.connection.execute(
            """
            SELECT contact.plan_version, event.action FROM contact_records contact
            JOIN care_plan_events event ON event.plan_id = contact.plan_id AND event.plan_version = contact.plan_version
            WHERE contact.plan_id = %s ORDER BY contact.occurred_at
            """,
            (plan["id"],),
        ).fetchall()
        self.assertEqual([row["plan_version"] for row in linked], event_versions)
        self.assertEqual([row["action"] for row in linked], ["CONFIRMED"] + ["EXECUTION_UPDATED"] * 3)
        completed = self.request("GET", "/plans", params={
            "week_start": self.week, "execution_status": "CONFIRMED",
        })["items"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["last_success_at"], last_success)
        self.assertEqual(completed[0]["version"], plan_version)

        unlinked = {key: value for key, value in payload.items() if key not in {"plan_id", "plan_version"}}
        unlinked.update(request_key=str(uuid4()), expected_version=current_version)
        self.assertNotIn("plan", self.request("POST", path, payload=unlinked, expected=201))

    def verify_rollback(self):
        with self.connect() as connection, connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            for table, field, ids in (
                ("organizations", "id", [self.org]),
                ("regions", "id", [self.district, self.dong]),
                ("users", "id", [self.worker, self.other_worker]),
                ("clients", "id", self.client_ids),
                ("client_assignments", "client_id", self.client_ids),
                ("plan_batches", "id", self.batch_ids),
                ("care_plans", "id", self.plan_ids),
                ("care_plan_events", "plan_id", self.plan_ids),
                ("contact_records", "client_id", self.client_ids),
                ("mutation_requests", "actor_id", [self.worker, self.other_worker]),
            ):
                if ids:
                    row = connection.execute(
                        f"SELECT count(*) AS total FROM {table} WHERE {field} = ANY(%s)", (ids,),
                    ).fetchone()
                    self.assertEqual(row["total"], 0, table)


if __name__ == "__main__":
    unittest.main()
