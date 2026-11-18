"""R07 통합 검증. R07_TEST_DATABASE_URL은 시드를 넣은 일회용 로컬 DB를 지정합니다."""

import asyncio
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from hashlib import sha256
from threading import Barrier
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from config.settings import get_settings
from main import app
from util.errors import ApiError

TEST_DATABASE_URL = os.getenv("R07_TEST_DATABASE_URL")
WORKER_ID = UUID("bedfe465-7ea8-5356-a760-785d68f2252a")
SEED_CLIENT_ID = "21ecb97b-ebc5-5a07-b88d-85511ebe793a"
SEOUL = ZoneInfo("Asia/Seoul")


@unittest.skipUnless(TEST_DATABASE_URL, "일회용 R07_TEST_DATABASE_URL이 필요합니다.")
class ContactDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database = conninfo_to_dict(TEST_DATABASE_URL)
        if (
            database.get("host") != "127.0.0.1"
            or database.get("dbname") != "meonjeogayo_r07_test"
            or database.get("hostaddr") not in (None, "127.0.0.1")
            or "service" in database
            or os.getenv("PGSERVICE")
            or os.getenv("PGHOSTADDR") not in (None, "127.0.0.1")
        ):
            raise ValueError("R07 쓰기 테스트는 로컬 meonjeogayo_r07_test에만 실행합니다.")

    def setUp(self):
        self.client_id = uuid4()
        self.assignment_id = uuid4()
        self.session_id = uuid4()
        self.session_token = uuid4().hex
        self.request_keys = []
        self.checklist_version_id = None
        self.now = datetime.now(SEOUL).replace(microsecond=0)
        self.client_path = f"/clients/{self.client_id}"
        self.form_path = f"{self.client_path}/contact-form"
        self.contact_path = f"{self.client_path}/contacts"
        settings = get_settings().model_copy(update={
            "database_url": TEST_DATABASE_URL,
            "dev_user_id": None,
            "contact_write_dev_user_id": None,
        })
        self.enterContext(patch("config.database.get_settings", return_value=settings))
        self.auth_settings = self.enterContext(patch("util.authentication.get_settings", return_value=settings))
        self.cookie = f"{settings.session_cookie_name}={self.session_token}"
        with self.connect() as conn:
            self.client = conn.execute(
                """
                INSERT INTO clients (id, organization_id, region_id, client_number, name, source_label)
                SELECT %s, organization_id, region_id, %s, 'R07 통합 테스트', 'R07 통합 테스트'
                FROM clients WHERE id = %s
                RETURNING version, input_version, organization_id, region_id
                """,
                (self.client_id, f"R07-{self.client_id}", SEED_CLIENT_ID),
            ).fetchone()
            self.assertIsNotNone(self.client, "개발 시드가 필요합니다.")
            conn.execute(
                """
                INSERT INTO client_assignments (id, client_id, worker_id, starts_at, reason)
                VALUES (%s, %s, %s, %s, 'R07 통합 테스트')
                """,
                (self.assignment_id, self.client_id, WORKER_ID, self.now - timedelta(days=3)),
            )
            conn.execute(
                "INSERT INTO user_sessions (id, user_id, token_hash, expires_at) VALUES (%s, %s, %s, %s)",
                (self.session_id, WORKER_ID, sha256(self.session_token.encode()).hexdigest(), self.now + timedelta(hours=1)),
            )
        self.addCleanup(self.remove_fixtures)

    def connect(self):
        return psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row, connect_timeout=5)

    def remove_fixtures(self):
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM contact_answers WHERE contact_id IN (SELECT id FROM contact_records WHERE client_id = %s)",
                (self.client_id,),
            )
            conn.execute("DELETE FROM contact_records WHERE client_id = %s", (self.client_id,))
            conn.execute(
                "DELETE FROM mutation_requests WHERE actor_id = %s AND operation = 'CONTACT_CREATE' AND request_key = ANY(%s)",
                (WORKER_ID, self.request_keys),
            )
            if self.checklist_version_id is not None:
                conn.execute("DELETE FROM checklist_questions WHERE checklist_version_id = %s", (self.checklist_version_id,))
                conn.execute("DELETE FROM checklist_versions WHERE id = %s", (self.checklist_version_id,))
            conn.execute("DELETE FROM client_assignments WHERE id = %s", (self.assignment_id,))
            conn.execute("DELETE FROM clients WHERE id = %s", (self.client_id,))
            conn.execute("DELETE FROM user_sessions WHERE id = %s", (self.session_id,))

    def request(self, method, path, payload=None, *, authenticated=True, params=None):
        async def call():
            messages = []
            headers = [(b"cookie", self.cookie.encode())] if authenticated else []
            if payload is not None:
                headers.append((b"content-type", b"application/json"))

            async def receive():
                body = b"" if payload is None else json.dumps(payload).encode()
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await app(
                {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": method, "scheme": "http", "path": path, "root_path": "",
                    "query_string": urlencode(params or {}).encode(), "headers": headers,
                    "client": ("127.0.0.1", 12345), "server": ("test", 80),
                },
                receive,
                send,
            )
            start = next(message for message in messages if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            return start["status"], json.loads(body)

        return asyncio.run(call())

    def get_data(self, path, params=None):
        status, body = self.request("GET", path, params=params)
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["error"])
        return body["data"]

    def new_payload(self, *, occurred_at=None, result="SUCCESS", channel="VISIT"):
        form = self.get_data(self.form_path)
        occurred_at = occurred_at or self.now - timedelta(days=1)
        request_key = str(uuid4())
        self.request_keys.append(request_key)
        return {
            "request_key": request_key,
            "expected_version": form["client_version"],
            "checklist_version_id": form["checklist_version_id"],
            "answers": [{
                "question_id": form["questions"][0]["question_id"],
                "answer_code": "RISK_FOUND" if result == "SUCCESS" else "UNCONFIRMED",
                "note": "테스트 답변",
            }],
            "channel": channel,
            "occurred_at": occurred_at.isoformat(),
            "result": result,
            "memo": "테스트 연락 기록",
            "next_check_date": (occurred_at.date() + timedelta(days=4)).isoformat(),
            "no_action_reason": "연락이 닿지 않음" if result != "SUCCESS" else None,
        }

    def post(self, payload, **kwargs):
        return self.request("POST", self.contact_path, payload, **kwargs)

    def assert_error(self, response, status_code, error_code):
        status, body = response
        self.assertEqual(status, status_code, body)
        self.assertIsNone(body["data"])
        self.assertEqual(body["error"]["code"], error_code)

    def assert_saved_rows(self, count):
        with self.connect() as conn:
            saved = conn.execute(
                """
                SELECT version, input_version, calculation_status,
                    (SELECT count(*) FROM contact_records WHERE client_id = clients.id) AS contacts,
                    (SELECT count(*) FROM contact_answers AS answer JOIN contact_records AS contact
                        ON contact.id = answer.contact_id WHERE contact.client_id = clients.id) AS answers,
                    (SELECT count(*) FROM mutation_requests WHERE actor_id = %s AND operation = 'CONTACT_CREATE'
                        AND request_key = ANY(%s)) AS mutations
                FROM clients WHERE id = %s
                """,
                (WORKER_ID, self.request_keys, self.client_id),
            ).fetchone()
        self.assertEqual(saved["version"], self.client["version"] + count)
        self.assertEqual(saved["input_version"], self.client["input_version"] + count)
        self.assertEqual(saved["calculation_status"], "PENDING")
        self.assertEqual((saved["contacts"], saved["answers"], saved["mutations"]), (count, count, count))

    def test_save_and_replay_then_read_detail_and_history(self):
        success = self.new_payload(occurred_at=self.now - timedelta(days=2))
        status, first = self.post(success)
        self.assertEqual(status, 201, first)
        self.assertEqual(first["data"]["client_version"], success["expected_version"] + 1)
        self.assertEqual(first["data"]["calculation_status"], "PENDING")
        for result, delay in (("FAILED", timedelta(days=1)), ("REFUSED", timedelta(minutes=5))):
            later = self.new_payload(result=result, channel="PHONE", occurred_at=self.now - delay)
            status, response = self.post(later)
            self.assertEqual(status, 201, response)
            self.assertEqual(datetime.fromisoformat(response["data"]["last_success_at"]), datetime.fromisoformat(success["occurred_at"]))
        self.assertEqual(self.post(success), (201, first))
        self.assert_error(self.post(dict(success, memo="다른 입력")), 409, "REQUEST_KEY_CONFLICT")
        older = self.new_payload(occurred_at=self.now - timedelta(days=2, hours=1))
        status, response = self.post(older)
        self.assertEqual(status, 201, response)
        self.assertEqual(response["data"]["next_check_date"], later["next_check_date"])
        detail = self.get_data(self.client_path)
        self.assertEqual(datetime.fromisoformat(detail["last_success_at"]), datetime.fromisoformat(success["occurred_at"]))
        self.assertEqual(datetime.fromisoformat(detail["last_attempt_at"]), datetime.fromisoformat(later["occurred_at"]))
        self.assertEqual(detail["next_check_date"], later["next_check_date"])
        history = self.get_data(f"{self.client_path}/history", {"event_type": "CONTACT"})
        self.assertEqual([item["detail"]["result"] for item in history["items"]], ["REFUSED", "FAILED", "SUCCESS", "SUCCESS"])
        self.assertEqual(history["items"][2]["event_id"], first["data"]["contact_id"])
        self.assertEqual(history["items"][0]["detail"]["answers"][0]["answer_code"], "UNCONFIRMED")
        self.assertIsNone(history["items"][0]["detail"]["plan_id"])
        with self.connect() as conn:
            saved = conn.execute(
                "SELECT assignment_id, organization_id, region_id FROM contact_records WHERE id = %s",
                (first["data"]["contact_id"],),
            ).fetchone()
        self.assertEqual(saved, {
            "assignment_id": self.assignment_id, "organization_id": self.client["organization_id"],
            "region_id": self.client["region_id"],
        })
        self.assert_saved_rows(4)

    def test_public_development_identity_cannot_write(self):
        payload = self.new_payload()
        self.auth_settings.return_value.dev_user_id = WORKER_ID
        self.assertEqual(self.request("GET", self.form_path, authenticated=False)[0], 200)
        self.assert_error(self.post(payload, authenticated=False), 401, "AUTH_REQUIRED")
        self.assert_saved_rows(0)

    def test_replay_rechecks_current_assignment(self):
        payload = self.new_payload()
        self.assertEqual(self.post(payload)[0], 201)
        with self.connect() as conn:
            conn.execute("UPDATE client_assignments SET ends_at = %s WHERE id = %s", (self.now, self.assignment_id))
        self.assert_error(self.post(payload), 404, "CLIENT_NOT_FOUND")
        self.assert_saved_rows(1)

    def test_concurrent_retries_write_once_and_new_keys_conflict(self):
        payload = self.new_payload()
        results = self.post_in_parallel(payload, payload)
        self.assertEqual(results[0][0], 201, results)
        self.assertEqual(results[0], results[1])
        first, second = self.new_payload(), self.new_payload()
        results = self.post_in_parallel(first, second)
        self.assertEqual(sorted(status for status, _ in results), [201, 409])
        self.assert_error(next(response for response in results if response[0] == 409), 409, "VERSION_CONFLICT")
        self.assert_saved_rows(2)

    def post_in_parallel(self, *payloads):
        barrier = Barrier(len(payloads), timeout=10)

        def send(payload):
            barrier.wait()
            return self.post(payload)

        with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
            return list(executor.map(send, payloads, timeout=20))

    def test_failure_rolls_back_rows_and_same_key_can_retry(self):
        payload = self.new_payload()
        failure = ApiError("SERVICE_UNAVAILABLE", "응답 처리 실패", status_code=503)
        with patch("handler.contact_handler.success_response", side_effect=failure):
            self.assert_error(self.post(payload), 503, "SERVICE_UNAVAILABLE")
        self.assert_saved_rows(0)
        self.assertEqual(self.post(payload)[0], 201)
        self.assert_saved_rows(1)

    def test_reject_invalid_contact_dates_and_answers(self):
        payload = self.new_payload(result="FAILED", channel="PHONE")
        wrong_answer = dict(payload["answers"][0], answer_code="NO_RISK")
        invalid_payloads = (
            {key: value for key, value in payload.items() if key != "next_check_date"},
            dict(payload, occurred_at=(self.now + timedelta(hours=1)).isoformat()),
            dict(payload, occurred_at=(self.now - timedelta(days=4)).isoformat()),
            dict(payload, next_check_date=(self.now.date() - timedelta(days=2)).isoformat()),
            dict(payload, answers=[wrong_answer]),
            dict(payload, answers=[dict(payload["answers"][0], question_id=str(uuid4()))]),
            dict(payload, answers=payload["answers"] * 2),
            dict(payload, memo="가" * 2_001),
        )
        for index, invalid in enumerate(invalid_payloads):
            with self.subTest(index=index):
                self.assert_error(self.post(invalid), 422, "INVALID_REQUEST")
        self.assert_saved_rows(0)

    def test_reject_changed_forms_and_check_required_choices(self):
        payload = self.new_payload()
        self.checklist_version_id = uuid4()
        question_id = uuid4()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO checklist_versions (id, version_code, name, effective_from, published_at) VALUES (%s, %s, '시험 양식', now(), now())",
                (self.checklist_version_id, f"R07-{self.checklist_version_id}"),
            )
            conn.execute(
                """
                INSERT INTO checklist_questions (id, checklist_version_id, question_code, label, sort_order, is_required, allowed_answers)
                VALUES (%s, %s, 'REQUIRED', '필수 문항', 1, true, ARRAY['RISK_FOUND','UNCONFIRMED'])
                """,
                (question_id, self.checklist_version_id),
            )
        refreshed = self.get_data(self.form_path)
        self.assertEqual(refreshed["checklist_version_id"], str(self.checklist_version_id))
        question = refreshed["questions"][0]
        self.assertEqual(question["question_id"], str(question_id))
        self.assertTrue(question["is_required"])
        self.assertEqual(question["allowed_answers"], ["RISK_FOUND", "UNCONFIRMED"])
        self.assert_error(self.post(payload), 409, "CHECKLIST_VERSION_CONFLICT")
        current = dict(payload, checklist_version_id=str(self.checklist_version_id))
        self.assert_error(self.post(dict(current, answers=[])), 422, "INVALID_REQUEST")
        disallowed = {"question_id": str(question_id), "answer_code": "NO_RISK"}
        self.assert_error(self.post(dict(current, answers=[disallowed])), 422, "INVALID_REQUEST")
        self.assert_saved_rows(0)
