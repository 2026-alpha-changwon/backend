"""R07 방문·전화 기록을 요청 단위 쓰기 트랜잭션에서 저장합니다."""

from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from dto.contact_dto import ContactCreateRequest, ContactCreatedData
from service.contact_service import (
    get_checklist_questions,
    get_contact_client,
    get_current_checklist,
)
from util.errors import ApiError


def create_contact(
    connection: Connection, user_id: UUID, client_id: UUID, payload: ContactCreateRequest,
) -> ContactCreatedData:
    # 대상자를 먼저 잠그고 현재 권한을 확인합니다. 기존 요청 재시도에도 적용합니다.
    client = get_contact_client(connection, user_id, client_id, lock=True)
    request_payload = _request_payload(client_id, payload)
    request_id = uuid4()
    claimed = connection.execute(
        """
        INSERT INTO mutation_requests (
            id, actor_id, operation, request_key, request_payload, result_payload, completed_at
        )
        VALUES (%s, %s, 'CONTACT_CREATE', %s, %s, '{}'::jsonb, transaction_timestamp())
        ON CONFLICT (actor_id, operation, request_key) DO NOTHING
        RETURNING id
        """,
        (request_id, user_id, payload.request_key, Jsonb(request_payload)),
    ).fetchone()
    if claimed is None:
        # READ COMMITTED의 다음 문장에서 경쟁 요청이 커밋한 결과를 읽습니다.
        previous = connection.execute(
            """
            SELECT request_payload, result_payload FROM mutation_requests
            WHERE actor_id = %s AND operation = 'CONTACT_CREATE' AND request_key = %s
            """,
            (user_id, payload.request_key),
        ).fetchone()
        if previous["request_payload"] != request_payload:
            raise ApiError(
                "REQUEST_KEY_CONFLICT", "같은 요청 키에 다른 입력을 사용할 수 없습니다.", status_code=409,
            )
        return ContactCreatedData.model_validate(previous["result_payload"]["response"])

    if client["version"] != payload.expected_version:
        raise _version_conflict()

    # 현재 게시 양식을 확인하고 실제 연락 날짜는 한국 시간으로 검증합니다.
    checklist = get_current_checklist(connection)
    if checklist["checklist_version_id"] != payload.checklist_version_id:
        raise ApiError(
            "CHECKLIST_VERSION_CONFLICT", "체크리스트가 변경되었습니다. 최신 양식을 확인해 주세요.",
            status_code=409,
        )
    questions = get_checklist_questions(connection, payload.checklist_version_id)
    _validate_answers(payload, questions)
    assignment_id = _contact_assignment(connection, user_id, client, payload)

    contact_id = uuid4()
    _insert_contact(connection, user_id, client_id, client, assignment_id, contact_id, request_id, payload)
    updated = connection.execute(
        """
        UPDATE clients
        SET updated_at = greatest(updated_at, clock_timestamp()),
            version = version + 1,
            input_version = input_version + 1,
            calculation_status = 'PENDING',
            calculation_run_id = NULL,
            calculation_started_at = NULL,
            calculation_finished_at = NULL,
            calculation_error_code = NULL
        WHERE id = %s AND version = %s
        RETURNING version
        """,
        (client_id, payload.expected_version),
    ).fetchone()
    if updated is None:
        raise _version_conflict()

    result = ContactCreatedData(
        contact_id=contact_id, client_id=client_id, client_version=updated["version"],
        **_contact_dates(connection, client_id),
    )
    result_payload = {
        "schema_version": 1,
        "resource_ids": [str(contact_id)],
        "response": result.model_dump(mode="json"),
    }
    connection.execute(
        """
        UPDATE mutation_requests
        SET result_payload = %s, completed_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE id = %s
        """,
        (Jsonb(result_payload), request_id),
    )
    return result


def _request_payload(client_id: UUID, payload: ContactCreateRequest) -> dict:
    body = payload.model_dump(mode="json", exclude={"request_key", "expected_version"})
    body["answers"] = sorted(body["answers"], key=lambda answer: answer["question_id"])
    return {
        "schema_version": 1,
        "target_id": str(client_id),
        "expected_version": payload.expected_version,
        "body": body,
    }


def _version_conflict() -> ApiError:
    return ApiError(
        "VERSION_CONFLICT", "대상자 정보가 변경되었습니다. 최신 정보를 확인해 주세요.", status_code=409,
    )


def _validate_answers(payload: ContactCreateRequest, questions: list[dict]) -> None:
    by_id = {question["question_id"]: question for question in questions}
    answered = set()
    for answer in payload.answers:
        question = by_id.get(answer.question_id)
        if question is None:
            raise ApiError("INVALID_REQUEST", "선택한 양식에 없는 문항입니다.", status_code=422)
        if answer.question_id in answered:
            raise ApiError("INVALID_REQUEST", "같은 문항에 여러 번 답할 수 없습니다.", status_code=422)
        if answer.answer_code not in question["allowed_answers"]:
            raise ApiError("INVALID_REQUEST", "문항에 허용된 선택지를 입력해 주세요.", status_code=422)
        if payload.result != "SUCCESS" and answer.answer_code == "NO_RISK":
            raise ApiError(
                "INVALID_REQUEST", "연락 실패·거부의 문항은 위험 없음으로 저장할 수 없습니다.", status_code=422,
            )
        answered.add(answer.question_id)
    if any(question["is_required"] and question["question_id"] not in answered for question in questions):
        raise ApiError("INVALID_REQUEST", "필수 체크리스트 문항에 답해 주세요.", status_code=422)


def _contact_assignment(
    connection: Connection, user_id: UUID, client: dict, payload: ContactCreateRequest,
) -> UUID:
    dates = connection.execute(
        """
        SELECT %(occurred_at)s <= clock_timestamp() AS valid_time,
               (%(occurred_at)s::timestamptz AT TIME ZONE 'Asia/Seoul')::date
                   <= %(next_check_date)s AS valid_next_date
        """,
        {"occurred_at": payload.occurred_at, "next_check_date": payload.next_check_date},
    ).fetchone()
    if not dates["valid_time"]:
        raise ApiError("INVALID_REQUEST", "실제 연락 시각에 미래 시각을 입력할 수 없습니다.", status_code=422)
    if not dates["valid_next_date"]:
        raise ApiError("INVALID_REQUEST", "다음 확인일은 실제 연락일보다 앞설 수 없습니다.", status_code=422)

    assignment = connection.execute(
        """
        SELECT assignment.id
        FROM client_assignments assignment
        JOIN regions region ON region.id = %(region_id)s
        JOIN regions district ON district.id = region.parent_id
        CROSS JOIN (SELECT (%(occurred_at)s::timestamptz AT TIME ZONE 'Asia/Seoul')::date AS occurred_on) contact
        WHERE assignment.client_id = %(client_id)s AND assignment.worker_id = %(user_id)s
          AND assignment.starts_at <= %(occurred_at)s
          AND (assignment.ends_at IS NULL OR %(occurred_at)s < assignment.ends_at)
          AND region.valid_from <= contact.occurred_on
          AND (region.valid_to IS NULL OR contact.occurred_on < region.valid_to)
          AND district.valid_from <= contact.occurred_on
          AND (district.valid_to IS NULL OR contact.occurred_on < district.valid_to)
        FOR SHARE OF assignment
        """,
        {
            "client_id": client["id"], "user_id": user_id, "region_id": client["region_id"],
            "occurred_at": payload.occurred_at,
        },
    ).fetchone()
    if assignment is None:
        raise ApiError(
            "INVALID_REQUEST", "실제 연락 시각의 대상자 배정과 지역을 확인해 주세요.", status_code=422,
        )
    return assignment["id"]


def _insert_contact(
    connection: Connection, user_id: UUID, client_id: UUID, client: dict,
    assignment_id: UUID, contact_id: UUID, request_id: UUID, payload: ContactCreateRequest,
) -> None:
    connection.execute(
        """
        INSERT INTO contact_records (
            id, client_id, worker_id, assignment_id, organization_id, region_id,
            checklist_version_id, channel, result, occurred_at, crisis_detected,
            memo, next_check_date, no_action_reason, mutation_request_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            contact_id, client_id, user_id, assignment_id, client["organization_id"], client["region_id"],
            payload.checklist_version_id, payload.channel, payload.result, payload.occurred_at,
            payload.crisis_detected, payload.memo, payload.next_check_date, payload.no_action_reason,
            request_id,
        ),
    )
    if payload.answers:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO contact_answers (id, contact_id, question_id, answer_code, note)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (uuid4(), contact_id, answer.question_id, answer.answer_code, answer.note)
                    for answer in payload.answers
                ],
            )


def _contact_dates(connection: Connection, client_id: UUID) -> dict:
    return connection.execute(
        """
        SELECT max(occurred_at) FILTER (WHERE result = 'SUCCESS') AS last_success_at,
               max(occurred_at) AS last_attempt_at,
               (
                   SELECT next_check_date FROM contact_records WHERE client_id = %(client_id)s
                   ORDER BY occurred_at DESC, created_at DESC, id DESC LIMIT 1
               ) AS next_check_date
        FROM contact_records
        WHERE client_id = %(client_id)s
        """,
        {"client_id": client_id},
    ).fetchone()
