"""돌봄 계획의 업무 권한과 변경 이력을 관리합니다."""

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from dto.plan_dto import PlanUpdatedData, PlanUpdateRequest
from service.client_service import VISIBLE_CLIENTS_CTE
from util.errors import ApiError


_PLAN_STATE_COLUMNS = (
    "version",
    "client_id",
    "assignment_id",
    "week_start",
    "batch_id",
    "risk_snapshot_id",
    "recommended_channel",
    "channel",
    "rank",
    "recommendation_reason",
    "review_status",
    "execution_status",
    "scheduled_at",
    "defer_reason",
    "channel_change_reason",
)


def update_plan(
    connection: Connection, user_id: UUID, plan_id: UUID, body: PlanUpdateRequest,
) -> PlanUpdatedData:
    plan = lock_visible_plan(connection, user_id, plan_id)
    request_payload = {
        "schema_version": 1,
        "target_id": str(plan_id),
        "expected_version": body.expected_version,
        "body": body.model_dump(mode="json", exclude={"request_key", "expected_version"}),
    }
    request_id = uuid4()
    claimed = connection.execute(
        """
        INSERT INTO mutation_requests (
            id, actor_id, operation, request_key, request_payload, result_payload, completed_at
        )
        VALUES (%s, %s, 'PLAN_UPDATE', %s, %s, '{}'::jsonb, transaction_timestamp())
        ON CONFLICT (actor_id, operation, request_key) DO NOTHING
        RETURNING id
        """,
        (request_id, user_id, body.request_key, Jsonb(request_payload)),
    ).fetchone()
    if claimed is None:
        previous = connection.execute(
            """
            SELECT request_payload, result_payload FROM mutation_requests
            WHERE actor_id = %s AND operation = 'PLAN_UPDATE' AND request_key = %s
            """,
            (user_id, body.request_key),
        ).fetchone()
        if previous["request_payload"] != request_payload:
            raise ApiError(
                "REQUEST_KEY_CONFLICT", "같은 요청 키에 다른 입력을 사용할 수 없습니다.", status_code=409,
            )
        return PlanUpdatedData.model_validate(previous["result_payload"]["response"])

    if plan["version"] != body.expected_version:
        raise _version_conflict()

    changes, action = _plan_changes(plan, body)
    if any(plan[column] != value for column, value in changes.items()):
        updated = connection.execute(
            """
            UPDATE care_plans
            SET review_status = %(review_status)s, channel = %(channel)s,
                scheduled_at = %(scheduled_at)s, defer_reason = %(defer_reason)s,
                channel_change_reason = %(channel_change_reason)s,
                updated_at = greatest(updated_at, clock_timestamp()), version = version + 1
            WHERE id = %(id)s AND version = %(expected_version)s
            RETURNING *
            """,
            {**changes, "id": plan_id, "expected_version": body.expected_version},
        ).fetchone()
        if updated is None:
            raise _version_conflict()
        record_plan_event(
            connection, plan_before=plan, plan_after=updated, actor_id=user_id,
            request_id=request_id, action=action, reason=body.reason,
        )
        plan = updated

    result = PlanUpdatedData(
        plan_id=plan_id, version=plan["version"], channel=plan["channel"], review_status=plan["review_status"],
        execution_status=plan["execution_status"], scheduled_at=plan["scheduled_at"],
        defer_reason=plan["defer_reason"], channel_change_reason=plan["channel_change_reason"],
    )
    result_payload = {
        "schema_version": 1,
        "resource_ids": [str(plan_id)],
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


def _plan_changes(plan: dict[str, Any], body: PlanUpdateRequest) -> tuple[dict[str, Any], str]:
    if plan["execution_status"] == "CONFIRMED":
        raise ApiError("PLAN_STATE_CONFLICT", "확인이 완료된 계획은 변경할 수 없습니다.", status_code=409)

    if body.action == "DEFER":
        if not body.reason or body.channel is not None or body.scheduled_at is not None:
            raise ApiError("INVALID_REQUEST", "보류 요청에는 사유를 입력해 주세요.", status_code=422)
        return {
            "review_status": "DEFERRED",
            "channel": plan["channel"],
            "scheduled_at": None,
            "defer_reason": body.reason,
            "channel_change_reason": plan["channel_change_reason"],
        }, "DEFERRED"

    if body.action == "CONFIRM":
        if plan["review_status"] == "CONFIRMED":
            raise ApiError("PLAN_STATE_CONFLICT", "이미 확정된 계획은 일정 변경을 사용해 주세요.", status_code=409)
        if body.channel is None or body.scheduled_at is None:
            raise ApiError("INVALID_REQUEST", "확정하려면 연락 방식과 예정일시를 입력해 주세요.", status_code=422)
        action = "CONFIRMED"
    else:
        if plan["review_status"] != "CONFIRMED":
            raise ApiError("PLAN_STATE_CONFLICT", "확정된 계획의 일정과 방식을 변경할 수 있습니다.", status_code=409)
        if body.channel is None and body.scheduled_at is None:
            raise ApiError("INVALID_REQUEST", "변경할 연락 방식이나 예정일시를 입력해 주세요.", status_code=422)
        action = "CHANNEL_CHANGED" if body.channel not in (None, plan["channel"]) else "RESCHEDULED"

    channel = body.channel or plan["channel"]
    if channel not in ("VISIT", "PHONE"):
        raise ApiError("INVALID_REQUEST", "확정할 연락 방식은 방문 또는 전화로 지정해 주세요.", status_code=422)

    channel_change_reason = None
    if channel != plan["recommended_channel"]:
        channel_change_reason = body.reason if channel != plan["channel"] else plan["channel_change_reason"]
        if not channel_change_reason:
            raise ApiError("INVALID_REQUEST", "추천 연락 방식 변경 사유를 입력해 주세요.", status_code=422)

    return {
        "review_status": "CONFIRMED",
        "channel": channel,
        "scheduled_at": body.scheduled_at or plan["scheduled_at"],
        "defer_reason": None,
        "channel_change_reason": channel_change_reason,
    }, action


def _version_conflict() -> ApiError:
    return ApiError("VERSION_CONFLICT", "계획이 변경되었습니다. 최신 정보를 확인해 주세요.", status_code=409)


def lock_visible_plan(
    connection: Connection, user_id: UUID, plan_id: UUID, *, client_id: UUID | None = None,
) -> dict[str, Any]:
    params = {"user_id": user_id, "plan_id": plan_id, "client_id": client_id}
    # 연락 기록 저장도 대상자 행을 먼저 잠급니다.
    client = connection.execute(
        VISIBLE_CLIENTS_CTE + """
        SELECT client.id
        FROM clients client
        JOIN visible_clients visible ON visible.id = client.id
        JOIN care_plans plan ON plan.client_id = client.id
        WHERE plan.id = %(plan_id)s
          AND (%(client_id)s::uuid IS NULL OR client.id = %(client_id)s)
        FOR NO KEY UPDATE OF client
        """,
        params,
    ).fetchone()
    if client is None:
        raise ApiError("PLAN_NOT_FOUND", "계획을 찾을 수 없습니다.", status_code=404)

    # 대기 중 배정이나 권한이 바뀌었을 수 있으므로 계획 잠금에서도 다시 확인합니다.
    plan = connection.execute(
        VISIBLE_CLIENTS_CTE + """
        SELECT plan.*
        FROM care_plans plan
        JOIN visible_clients client ON client.id = plan.client_id
        JOIN client_assignments assignment ON assignment.id = plan.assignment_id
                                          AND assignment.client_id = client.id
        JOIN plan_batches batch ON batch.id = plan.batch_id
                               AND batch.organization_id = client.organization_id
                               AND batch.week_start = plan.week_start
                               AND batch.as_of_at <= transaction_timestamp()
        WHERE plan.id = %(plan_id)s AND client.id = %(client_id)s
          AND assignment.worker_id = %(user_id)s
          AND assignment.starts_at <= transaction_timestamp()
          AND (assignment.ends_at IS NULL OR transaction_timestamp() < assignment.ends_at)
          AND (
              plan.risk_snapshot_id IS NULL OR EXISTS (
                  SELECT 1 FROM risk_snapshots snapshot
                  WHERE snapshot.id = plan.risk_snapshot_id AND snapshot.client_id = client.id
              )
          )
        FOR NO KEY UPDATE OF plan FOR SHARE OF assignment
        """,
        {**params, "client_id": client["id"]},
    ).fetchone()
    if plan is None:
        raise ApiError("PLAN_NOT_FOUND", "계획을 찾을 수 없습니다.", status_code=404)
    return plan


def plan_state(plan: dict[str, Any]) -> dict[str, Any]:
    state = {column: plan[column] for column in _PLAN_STATE_COLUMNS}
    for column, value in state.items():
        if isinstance(value, UUID):
            state[column] = str(value)
        elif isinstance(value, date):
            state[column] = value.isoformat()
    return state


def record_plan_event(
    connection: Connection, *, plan_before: dict[str, Any] | None, plan_after: dict[str, Any],
    actor_id: UUID | None, request_id: UUID | None, action: str, reason: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO care_plan_events (
            id, plan_id, plan_version, actor_id, occurred_at, action, reason,
            before_state, after_state, mutation_request_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            uuid4(), plan_after["id"], plan_after["version"], actor_id,
            plan_after["updated_at"], action, reason,
            Jsonb(plan_state(plan_before)) if plan_before is not None else None,
            Jsonb(plan_state(plan_after)), request_id,
        ),
    )
