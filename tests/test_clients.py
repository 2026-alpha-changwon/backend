"""공통 개발 seed를 가진 TEST_DATABASE_URL에서 R03을 읽기 전용으로 검증합니다."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from config.database import get_connection
from config.settings import get_settings
from main import app
from util.authentication import get_current_user_id
from util.errors import ApiError

TEST_DATABASE_URL = get_settings().test_database_url

WORKER_ID = UUID("bedfe465-7ea8-5356-a760-785d68f2252a")
OTHER_WORKER_ID = UUID("4ca0ab25-d3ff-559f-970b-ae861edb6439")
OTHER_ORGANIZATION_USER_ID = UUID("d4d7aa74-6aa4-53e3-a91b-7252e571eccb")
ADMIN_ID = UUID("c36408e9-a150-5692-b689-825cbd75f5f3")
REGION_ID = "60fc51c3-87f5-5899-8231-aa17acd2ed0e"
CLIENT_IDS = {
    1: "21ecb97b-ebc5-5a07-b88d-85511ebe793a",
    2: "c7aaef6e-8cec-5f91-8ad4-6af43f107e4c",
    3: "4ccdafed-1bd2-5961-9d34-055645e8f70a",
    4: "26e8d5ec-b9a0-52bb-9ccf-328d3b6a9345",
    9: "72dd8970-f4fe-5471-b05c-55f3c6092837",
    10: "75e0edfa-7d01-5258-bd3d-6f22afed5e5a",
    11: "f362cb6d-88c0-51c7-bba9-04ed64a5c684",
    12: "8476f1f6-f914-5a3a-a796-ff1b38da4df8",
}


class APIRequestCase(unittest.TestCase):
    def setUp(self):
        self.previous_overrides = app.dependency_overrides.copy()
        self.user_id = WORKER_ID
        app.dependency_overrides[get_current_user_id] = lambda: self.user_id
        app.dependency_overrides[get_connection] = lambda: None

    def tearDown(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.previous_overrides)

    def request(self, path, params=None):
        async def call():
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await app(
                {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "GET", "scheme": "http", "path": path, "root_path": "",
                    "query_string": urlencode(params or {}).encode(), "headers": [],
                    "client": ("127.0.0.1", 12345), "server": ("test", 80),
                },
                receive,
                send,
            )
            start = next(message for message in messages if message["type"] == "http.response.start")
            body = b"".join(
                message.get("body", b"")
                for message in messages if message["type"] == "http.response.body"
            )
            return start["status"], json.loads(body)

        return asyncio.run(call())

    def get_data(self, path="/clients", params=None):
        status, body = self.request(path, params)
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["error"])
        return body["data"]

    def assert_error(self, path, status_code, code, params=None):
        status, body = self.request(path, params)
        self.assertEqual(status, status_code, body)
        self.assertIsNone(body["data"])
        self.assertEqual(body["error"]["code"], code)
        self.assertIsInstance(body["error"]["message"], str)
        self.assertIsInstance(body["error"]["details"], list)
        return body["error"]


class ClientInputTests(APIRequestCase):
    def test_invalid_list_queries_use_common_error_response(self):
        for params in (
            {"page": 0}, {"page": "invalid"}, {"page_size": 0}, {"page_size": 101},
            {"region_id": "invalid"}, {"risk_grade": "   "},
            {"needs_benefit_review": "invalid"}, {"unchecked_days": 0},
            {"sort_by": "unknown"}, {"sort_order": "unknown"}, {"user_id": str(WORKER_ID)},
        ):
            with self.subTest(params=params):
                error = self.assert_error("/clients", 422, "INVALID_REQUEST", params)
                self.assertTrue(error["details"])
                self.assertIn("field", error["details"][0])
                self.assertIn("message", error["details"][0])

    def test_invalid_history_queries_use_common_error_response(self):
        path = f"/clients/{CLIENT_IDS[1]}/history"
        for params in (
            {"from_date": "2026-11-16", "to_date": "2026-11-15"},
            {"from_date": "2026-99-99"}, {"event_type": "unknown"}, {"page_size": 101},
        ):
            with self.subTest(params=params):
                self.assert_error(path, 422, "INVALID_REQUEST", params)

    def test_invalid_client_id_uses_common_error_response(self):
        self.assert_error("/clients/invalid", 422, "INVALID_REQUEST")
        self.assert_error("/clients/invalid/history", 422, "INVALID_REQUEST")

    def test_authentication_error_uses_common_error_response(self):
        def missing_user():
            raise ApiError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)

        app.dependency_overrides[get_current_user_id] = missing_user
        self.assert_error("/clients", 401, "AUTH_REQUIRED")

    def test_database_error_uses_common_error_response(self):
        def unavailable_connection():
            raise ApiError("SERVICE_UNAVAILABLE", "DB에 연결할 수 없습니다.", status_code=503)

        app.dependency_overrides[get_connection] = unavailable_connection
        self.assert_error("/clients", 503, "SERVICE_UNAVAILABLE")


@unittest.skipUnless(TEST_DATABASE_URL, "TEST_DATABASE_URL이 필요합니다.")
class ClientDatabaseTests(APIRequestCase):
    def setUp(self):
        super().setUp()

        def connection_override():
            with psycopg.connect(
                TEST_DATABASE_URL, row_factory=dict_row,
                autocommit=True, connect_timeout=5,
            ) as connection:
                connection.execute("SET TIME ZONE 'Asia/Seoul'")
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    yield connection

        app.dependency_overrides[get_connection] = connection_override

    def test_scope_and_default_order(self):
        data = self.get_data()
        self.assertEqual(data["total"], 8)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 20)
        self.assertEqual(data["data_mode"], "DEMO")
        self.assertEqual(datetime.fromisoformat(data["queried_at"]).utcoffset(), timedelta(hours=9))
        self.assertEqual(
            [item["client_number"] for item in data["items"]],
            [f"CW-2026-{number:04d}" for number in (6, 1, 4, 7, 3, 8, 5, 2)],
        )
        for item in data["items"]:
            self.assertIn(str(WORKER_ID), [worker["id"] for worker in item["workers"]])
            self.assertIsNone(item["risk"]["score"])
            self.assertIsNone(item["risk"]["grade"])
            self.assertEqual(item["risk"]["calculation_status"], "PENDING")

    def test_other_users_receive_their_assignments(self):
        for user_id, expected in (
            (OTHER_WORKER_ID, "CW-2026-0009"),
            (OTHER_ORGANIZATION_USER_ID, "CW-2026-0012"),
        ):
            with self.subTest(user_id=user_id):
                self.user_id = user_id
                data = self.get_data()
                self.assertEqual(data["total"], 1)
                self.assertEqual([item["client_number"] for item in data["items"]], [expected])

    def test_admin_has_no_implicit_personal_access(self):
        self.user_id = ADMIN_ID
        data = self.get_data()
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        self.assert_error(f"/clients/{CLIENT_IDS[1]}", 404, "CLIENT_NOT_FOUND")

    def test_out_of_scope_clients_and_history_return_404(self):
        for client_id in [CLIENT_IDS[number] for number in (9, 10, 11, 12)] + [str(UUID(int=0))]:
            for suffix in ("", "/history"):
                with self.subTest(client_id=client_id, suffix=suffix):
                    self.assert_error(f"/clients/{client_id}{suffix}", 404, "CLIENT_NOT_FOUND")

    def test_unknown_user_is_not_authenticated(self):
        self.user_id = UUID(int=0)
        self.assert_error("/clients", 401, "AUTH_REQUIRED")

    def test_search_and_combined_filters(self):
        self.assertEqual(self.get_data(params={"search": "김영수"})["total"], 2)
        self.assertEqual(self.get_data(params={"search": "  CW-2026-0007  "})["total"], 1)
        self.assertEqual(self.get_data(params={"search": ""})["total"], 8)
        self.assertEqual(self.get_data(params={"search": "%' OR TRUE --"})["total"], 0)
        data = self.get_data(params={"region_id": REGION_ID, "needs_benefit_review": "true"})
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["client_number"], "CW-2026-0003")
        self.assertEqual(self.get_data(params={"risk_grade": "UNPUBLISHED_GRADE"})["total"], 0)

    def test_benefit_review_false_includes_no_linked_benefits(self):
        data = self.get_data(params={"needs_benefit_review": "false"})
        self.assertEqual(data["total"], 7)
        self.assertNotIn("CW-2026-0003", [item["client_number"] for item in data["items"]])
        self.assertIn("CW-2026-0004", [item["client_number"] for item in data["items"]])

    def test_unchecked_filter_includes_never_successful_clients(self):
        all_clients = self.get_data()
        queried_at = datetime.fromisoformat(all_clients["queried_at"])
        oldest_success = min(
            datetime.fromisoformat(item["last_success_at"])
            for item in all_clients["items"] if item["last_success_at"] is not None
        )
        days = max(1, (queried_at - oldest_success).days + 2)
        data = self.get_data(params={"unchecked_days": days})
        self.assertEqual(data["total"], 6)
        self.assertTrue(all(item["last_success_at"] is None for item in data["items"]))

    def test_pagination_is_stable_and_preserves_total_on_empty_page(self):
        first = self.get_data(params={"page": 1, "page_size": 2})
        second = self.get_data(params={"page": 2, "page_size": 2})
        self.assertEqual(first["total"], 8)
        self.assertEqual(second["total"], 8)
        self.assertTrue({item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"]))
        empty = self.get_data(params={"page": 9, "page_size": 2})
        self.assertEqual(empty["total"], 8)
        self.assertEqual(empty["page"], 9)
        self.assertEqual(empty["items"], [])

    def test_contact_date_sorting_keeps_nulls_last(self):
        descending = self.get_data(params={"sort_by": "last_success_at", "sort_order": "desc"})
        self.assertEqual([item["client_number"] for item in descending["items"][:2]], ["CW-2026-0002", "CW-2026-0001"])
        self.assertTrue(all(item["last_success_at"] is None for item in descending["items"][2:]))
        ascending = self.get_data(params={"sort_by": "next_check_date", "sort_order": "asc"})
        self.assertEqual([item["client_number"] for item in ascending["items"][:3]], ["CW-2026-0003", "CW-2026-0001", "CW-2026-0002"])
        self.assertTrue(all(item["next_check_date"] is None for item in ascending["items"][3:]))

    def test_detail_uses_latest_profile_and_separates_success_from_attempt(self):
        data = self.get_data(f"/clients/{CLIENT_IDS[1]}")
        self.assertEqual(data["profile"]["revision"], 2)
        self.assertIs(data["profile"]["fall_history"], True)
        self.assertEqual(datetime.fromisoformat(data["last_success_at"]).date().isoformat(), "2026-11-03")
        self.assertEqual(datetime.fromisoformat(data["last_attempt_at"]).date().isoformat(), "2026-11-07")
        self.assertEqual(data["next_check_date"], "2026-11-08")
        self.assertEqual(data["phone_masked"], "010-****-1936")
        self.assertEqual(data["field_info"]["profile.fall_history"]["status"], "AVAILABLE")
        self.assertEqual(data["benefits"][0]["support_status"], "GUIDED")
        self.assertEqual(data["benefits"][0]["receipt_status"], "UNKNOWN")
        self.assertIsNone(data["benefits"][0]["receipt_verified_at"])
        self.assertIsNone(data["location"])

    def test_refused_contact_and_missing_contact_dates(self):
        refused = self.get_data(f"/clients/{CLIENT_IDS[3]}")
        self.assertIsNone(refused["last_success_at"])
        self.assertEqual(datetime.fromisoformat(refused["last_attempt_at"]).date().isoformat(), "2026-11-05")
        self.assertEqual(refused["next_check_date"], "2026-11-06")
        self.assertIs(refused["needs_benefit_review"], True)
        no_contact = self.get_data(f"/clients/{CLIENT_IDS[4]}")
        self.assertIsNone(no_contact["last_success_at"])
        self.assertIsNone(no_contact["last_attempt_at"])
        self.assertIsNone(no_contact["next_check_date"])
        self.assertEqual(no_contact["profile"]["health_flags"], [])
        self.assertEqual(no_contact["field_info"]["profile.health_flags"]["status"], "AVAILABLE")

    def test_history_contains_details_and_excludes_duplicate_referral_creation(self):
        data = self.get_data(f"/clients/{CLIENT_IDS[1]}/history")
        self.assertEqual(data["total"], 6)
        self.assertEqual(
            [item["event_type"] for item in data["items"]],
            ["REFERRAL_STATUS_CHANGE", "CONTACT", "BENEFIT", "REFERRAL_STATUS_CHANGE", "CONTACT", "REFERRAL_REQUEST"],
        )
        failed = data["items"][1]["detail"]
        self.assertEqual(failed["result"], "FAILED")
        self.assertEqual(failed["next_check_date"], "2026-11-08")
        self.assertIsNone(failed["crisis_detected"])
        self.assertEqual(failed["answers"][0]["answer_code"], "UNCONFIRMED")
        benefit = data["items"][2]["detail"]
        self.assertEqual(benefit["before_state"]["support_status"], "NEEDS_REVIEW")
        self.assertEqual(benefit["after_state"]["support_status"], "GUIDED")
        accepted = data["items"][3]["detail"]
        self.assertEqual(accepted["from_status"], "REQUESTED")
        self.assertEqual(accepted["to_status"], "IN_PROGRESS")
        self.assertEqual(accepted["current_status"], "COMPLETED")
        self.assertIsNone(accepted["change_result"])
        self.assertEqual(accepted["delivery_status"], "NOT_SENT")

    def test_history_type_date_and_empty_page_filters(self):
        path = f"/clients/{CLIENT_IDS[1]}/history"
        for event_type, total in (
            ("CONTACT", 2), ("BENEFIT", 1), ("REFERRAL_REQUEST", 1), ("REFERRAL_STATUS_CHANGE", 2),
        ):
            with self.subTest(event_type=event_type):
                data = self.get_data(path, {"event_type": event_type})
                self.assertEqual(data["total"], total)
                self.assertTrue(all(item["event_type"] == event_type for item in data["items"]))
        day = self.get_data(path, {"from_date": "2026-11-03", "to_date": "2026-11-03"})
        self.assertEqual(day["total"], 2)
        empty = self.get_data(path, {"page": 8, "page_size": 2})
        self.assertEqual(empty["total"], 6)
        self.assertEqual(empty["items"], [])
        no_history = self.get_data(f"/clients/{CLIENT_IDS[4]}/history")
        self.assertEqual(no_history["total"], 0)
        self.assertEqual(no_history["items"], [])


if __name__ == "__main__":
    unittest.main()
