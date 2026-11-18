"""R05 주간 계획과 오늘 확정된 방문을 조회합니다."""

from datetime import datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg import Connection

from dto.client_dto import LocationInfo
from dto.plan_dto import PlanListData, PlanListQuery, TodayVisitItem, TodayVisitListData
from service.client_service import VISIBLE_CLIENTS_CTE
from util.errors import ApiError


_SEOUL = ZoneInfo("Asia/Seoul")

_PLAN_ROWS = VISIBLE_CLIENTS_CTE + """
, plan_rows AS (
    SELECT plan.id AS plan_id, plan.version, plan.client_id,
           client.client_number, client.name AS client_name, client.region_id, client.region_name,
           client.latitude, client.longitude, plan.week_start, plan.batch_id, plan.risk_snapshot_id,
           plan.recommended_channel, plan.channel, plan.rank, plan.recommendation_reason,
           plan.review_status, plan.execution_status, plan.scheduled_at,
           plan.defer_reason, plan.channel_change_reason
    FROM visible_clients client
    JOIN care_plans plan ON plan.client_id = client.id
    JOIN plan_batches batch ON batch.id = plan.batch_id
                           AND batch.organization_id = client.organization_id
                           AND batch.week_start = plan.week_start
                           AND batch.as_of_at <= transaction_timestamp()
    JOIN client_assignments assignment ON assignment.id = plan.assignment_id
                                      AND assignment.client_id = client.id
                                      AND assignment.worker_id = %(user_id)s
                                      AND assignment.starts_at <= transaction_timestamp()
                                      AND (assignment.ends_at IS NULL OR transaction_timestamp() < assignment.ends_at)
)
"""


def _query_context(connection: Connection, user_id: UUID) -> dict:
    row = connection.execute(
        "SELECT organization_id, transaction_timestamp() AS queried_at FROM users WHERE id = %s AND is_active",
        (user_id,),
    ).fetchone()
    if row is None:
        raise ApiError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return row


def list_plans(connection: Connection, user_id: UUID, query: PlanListQuery) -> PlanListData:
    context = _query_context(connection, user_id)
    batch = connection.execute(
        """
        SELECT id, as_of_at, generation FROM plan_batches
        WHERE organization_id = %s AND week_start = %s AND as_of_at <= transaction_timestamp()
        ORDER BY generation DESC LIMIT 1
        """,
        (context["organization_id"], query.week_start),
    ).fetchone()
    items = []
    total = 0
    if batch is not None:
        params = {"user_id": user_id, "batch_id": batch["id"]}
        conditions = ["plan.batch_id = %(batch_id)s"]
        for field, value in (
            ("region_id", query.region_id),
            ("channel", query.channel),
            ("review_status", query.review_status),
            ("execution_status", query.execution_status),
        ):
            if value is not None:
                conditions.append(f"plan.{field} = %({field})s")
                params[field] = value
        where = " WHERE " + " AND ".join(conditions)
        total = connection.execute(
            _PLAN_ROWS + "SELECT count(*) AS total FROM plan_rows plan" + where,
            params,
        ).fetchone()["total"]
        offset = (query.page - 1) * query.page_size
        if offset < total:
            params.update(limit=query.page_size, offset=offset)
            items = connection.execute(
                _PLAN_ROWS + """
                SELECT plan.plan_id, plan.version, plan.client_id, plan.client_number, plan.client_name,
                       plan.region_id, plan.region_name, plan.recommended_channel, plan.channel,
                       plan.rank, snapshot.score, plan.recommendation_reason,
                       plan.review_status, plan.execution_status, plan.scheduled_at,
                       plan.defer_reason, plan.channel_change_reason,
                       (
                         SELECT max(contact.occurred_at) FROM contact_records contact
                         WHERE contact.client_id = plan.client_id AND contact.result = 'SUCCESS'
                       ) AS last_success_at
                FROM plan_rows plan
                LEFT JOIN risk_snapshots snapshot ON snapshot.id = plan.risk_snapshot_id
                                                 AND snapshot.client_id = plan.client_id
                """ + where + """
                ORDER BY plan.rank ASC NULLS LAST, plan.client_id ASC
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                params,
            ).fetchall()

    return PlanListData(
        week_start=query.week_start, batch=batch, queried_at=context["queried_at"],
        items=items, total=total, page=query.page, page_size=query.page_size,
    )


def list_today_visits(connection: Connection, user_id: UUID) -> TodayVisitListData:
    queried_at = _query_context(connection, user_id)["queried_at"]
    today = queried_at.astimezone(_SEOUL).date()
    starts_at = datetime.combine(today, time.min, tzinfo=_SEOUL)
    rows = connection.execute(
        _PLAN_ROWS + """
        SELECT plan_id, version, client_id, client_number, client_name, region_id, region_name,
               scheduled_at, execution_status, latitude, longitude
        FROM plan_rows
        WHERE channel = 'VISIT' AND review_status = 'CONFIRMED'
          AND scheduled_at >= %(starts_at)s AND scheduled_at < %(ends_at)s
        ORDER BY scheduled_at ASC, plan_id ASC
        """,
        {"user_id": user_id, "starts_at": starts_at, "ends_at": starts_at + timedelta(days=1)},
    ).fetchall()
    items = []
    for order, row in enumerate(rows, start=1):
        latitude = row.pop("latitude")
        longitude = row.pop("longitude")
        location = None
        if latitude is not None and longitude is not None:
            location = LocationInfo(latitude=latitude, longitude=longitude)
        items.append(TodayVisitItem(**row, location=location, order=order))
    return TodayVisitListData(today_date=today, queried_at=queried_at, items=items)
