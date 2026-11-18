"""대상자 접근 범위와 현장 기록 양식을 조회합니다."""

from typing import Any
from uuid import UUID

from psycopg import Connection

from dto.contact_dto import ContactFormData
from service.client_service import VISIBLE_CLIENTS_CTE
from util.errors import ApiError


def get_contact_client(
    connection: Connection,
    user_id: UUID,
    client_id: UUID,
    *,
    lock: bool = False,
) -> dict[str, Any]:
    params = {"user_id": user_id, "client_id": client_id}
    if lock:
        locked = connection.execute(
            VISIBLE_CLIENTS_CTE + """
            SELECT c.id
            FROM clients c
            JOIN visible_clients visible ON visible.id = c.id
            WHERE c.id = %(client_id)s
            FOR NO KEY UPDATE OF c
            """,
            params,
        ).fetchone()
        if locked is None:
            raise ApiError("CLIENT_NOT_FOUND", "대상자를 찾을 수 없습니다.", status_code=404)

    # 잠금 대기 중 다른 요청이 배정이나 대상자 버전을 바꿨을 수 있습니다.
    client = connection.execute(
        VISIBLE_CLIENTS_CTE + """
        SELECT id, version, organization_id, region_id
        FROM visible_clients WHERE id = %(client_id)s
        """,
        params,
    ).fetchone()
    if client is None:
        raise ApiError("CLIENT_NOT_FOUND", "대상자를 찾을 수 없습니다.", status_code=404)
    return client


def get_current_checklist(connection: Connection) -> dict[str, Any]:
    checklist = connection.execute(
        """
        SELECT id AS checklist_version_id, version_code, name
        FROM checklist_versions
        WHERE published_at <= transaction_timestamp()
          AND effective_from <= transaction_timestamp()
        ORDER BY effective_from DESC
        LIMIT 1
        """
    ).fetchone()
    if checklist is None:
        raise ApiError("CHECKLIST_UNAVAILABLE", "현재 사용할 수 있는 현장 기록 양식이 없습니다.", status_code=503)
    return checklist


def get_checklist_questions(
    connection: Connection, checklist_version_id: UUID,
) -> list[dict[str, Any]]:
    return connection.execute(
        """
        SELECT id AS question_id, question_code, label, sort_order, is_required, allowed_answers
        FROM checklist_questions
        WHERE checklist_version_id = %s
        ORDER BY sort_order
        """,
        (checklist_version_id,),
    ).fetchall()


def get_contact_form(connection: Connection, user_id: UUID, client_id: UUID) -> ContactFormData:
    client = get_contact_client(connection, user_id, client_id)
    checklist = get_current_checklist(connection)
    return ContactFormData(
        client_id=client["id"],
        client_version=client["version"],
        **checklist,
        questions=get_checklist_questions(connection, checklist["checklist_version_id"]),
    )
