"""R03 대상자 조회와 업무 권한 검사, 조회 SQL입니다."""

import re
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection

from dto.client_dto import (
    ClientDetailData,
    ClientHistoryData,
    ClientHistoryQuery,
    ClientListData,
    ClientListItem,
    ClientListQuery,
)
from util.errors import ApiError


# 기관과 사용자가 각각 구 또는 동을 허용할 수 있습니다.
VISIBLE_CLIENTS_CTE = """
WITH visible_clients AS (
    SELECT c.*, r.name AS region_name, district.name AS district_name,
           o.name AS organization_name
    FROM clients c
    JOIN organizations o ON o.id = c.organization_id
    JOIN regions r ON r.id = c.region_id
    JOIN regions district ON district.id = r.parent_id
    JOIN users requester ON requester.id = %(user_id)s
    WHERE requester.is_active
      AND requester.organization_id = c.organization_id
      AND r.valid_from <= (transaction_timestamp() AT TIME ZONE 'Asia/Seoul')::date
      AND (r.valid_to IS NULL OR (transaction_timestamp() AT TIME ZONE 'Asia/Seoul')::date < r.valid_to)
      AND district.valid_from <= (transaction_timestamp() AT TIME ZONE 'Asia/Seoul')::date
      AND (district.valid_to IS NULL OR (transaction_timestamp() AT TIME ZONE 'Asia/Seoul')::date < district.valid_to)
      AND EXISTS (
          SELECT 1 FROM organization_regions allowed
          WHERE allowed.organization_id = c.organization_id
            AND allowed.region_id IN (r.id, r.parent_id)
      )
      AND EXISTS (
          SELECT 1 FROM user_regions allowed
          WHERE allowed.user_id = requester.id
            AND allowed.region_id IN (r.id, r.parent_id)
      )
      AND EXISTS (
          SELECT 1 FROM client_assignments assignment
          WHERE assignment.client_id = c.id AND assignment.worker_id = requester.id
            AND assignment.starts_at <= transaction_timestamp()
            AND (assignment.ends_at IS NULL OR transaction_timestamp() < assignment.ends_at)
      )
)
"""

_CLIENT_QUERY = VISIBLE_CLIENTS_CTE + """
, client_rows AS (
    SELECT c.*,
           CASE WHEN c.birth_date <= (transaction_timestamp() AT TIME ZONE 'Asia/Seoul')::date
                THEN extract(year FROM age(
                    (transaction_timestamp() AT TIME ZONE 'Asia/Seoul')::date, c.birth_date
                ))::integer END AS age,
           to_jsonb(profile) AS profile,
           to_jsonb(snapshot) AS risk_snapshot,
           snapshot_model.version_code AS risk_model_version,
           CASE WHEN snapshot.id IS NULL THEN false ELSE
               snapshot.input_version <> c.input_version
               OR snapshot.run_id IS DISTINCT FROM c.calculation_run_id
               OR snapshot.risk_model_id IS DISTINCT FROM current_model.id
           END AS risk_stale,
           coalesce(factors.summary, '[]'::jsonb) AS risk_summary,
           coalesce(workers.items, '[]'::jsonb) AS workers,
           contacts.last_attempt_at, contacts.last_success_at,
           latest_contact.next_check_date, latest_contact.occurred_at AS next_date_checked_at,
           EXISTS (
               SELECT 1 FROM client_benefits benefit
               WHERE benefit.client_id = c.id AND benefit.support_status = 'NEEDS_REVIEW'
           ) AS needs_benefit_review
    FROM visible_clients c
    LEFT JOIN LATERAL (
        SELECT p.* FROM client_profiles p WHERE p.client_id = c.id
        ORDER BY p.revision DESC, p.id DESC LIMIT 1
    ) profile ON true
    LEFT JOIN LATERAL (
        SELECT model.id FROM risk_models model
        WHERE model.status = 'PUBLISHED' AND model.published_at <= transaction_timestamp()
        ORDER BY model.published_at DESC, model.id DESC LIMIT 1
    ) current_model ON true
    LEFT JOIN LATERAL (
        SELECT s.* FROM risk_snapshots s
        JOIN risk_models model ON model.id = s.risk_model_id
                              AND model.status = 'PUBLISHED'
                              AND model.published_at <= transaction_timestamp()
        WHERE s.client_id = c.id
        ORDER BY (s.run_id = c.calculation_run_id) DESC NULLS LAST,
                 s.input_version DESC, s.started_at DESC, s.calculated_at DESC, s.id DESC
        LIMIT 1
    ) snapshot ON true
    LEFT JOIN risk_models snapshot_model ON snapshot_model.id = snapshot.risk_model_id
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(factor.evidence_text ORDER BY model_factor.sort_order, factor.id) AS summary
        FROM risk_factors factor
        LEFT JOIN risk_model_factors model_factor
          ON model_factor.risk_model_id = snapshot.risk_model_id
         AND model_factor.factor_code = factor.factor_code
        WHERE factor.risk_snapshot_id = snapshot.id
    ) factors ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object('id', worker.id, 'name', worker.name)
                         ORDER BY worker.id) AS items
        FROM client_assignments assignment
        JOIN users worker ON worker.id = assignment.worker_id
        WHERE assignment.client_id = c.id AND assignment.starts_at <= transaction_timestamp()
          AND (assignment.ends_at IS NULL OR transaction_timestamp() < assignment.ends_at)
    ) workers ON true
    LEFT JOIN LATERAL (
        SELECT max(contact.occurred_at) AS last_attempt_at,
               max(contact.occurred_at) FILTER (WHERE contact.result = 'SUCCESS') AS last_success_at
        FROM contact_records contact WHERE contact.client_id = c.id
    ) contacts ON true
    LEFT JOIN LATERAL (
        SELECT contact.next_check_date, contact.occurred_at
        FROM contact_records contact WHERE contact.client_id = c.id
        ORDER BY contact.occurred_at DESC, contact.created_at DESC, contact.id DESC LIMIT 1
    ) latest_contact ON true
)
"""

_REFERRAL_DETAIL = """
jsonb_build_object(
    'referral_id', referral.id, 'contact_id', referral.contact_id,
    'target_organization', jsonb_build_object('id', target.id, 'name', target.name),
    'request_reason', referral.reason, 'requested_action', referral.requested_action,
    'due_date', referral.due_date, 'current_status', referral.status,
    'accepted_at', referral.accepted_at, 'completed_at', referral.completed_at,
    'rejected_at', referral.rejected_at, 'current_result', referral.result,
    'delivery_status', 'NOT_SENT'
)
"""

_HISTORY_QUERY = """
WITH events AS (
    SELECT contact.id AS event_id, 'CONTACT'::text AS event_type, contact.occurred_at,
           jsonb_build_object('id', worker.id, 'name', worker.name) AS worker,
           CASE contact.channel WHEN 'VISIT' THEN '방문' ELSE '전화' END || ' · ' ||
           CASE contact.result WHEN 'SUCCESS' THEN '확인 완료'
                               WHEN 'FAILED' THEN '연락 실패' ELSE '연락 거부' END AS summary,
           jsonb_build_object(
               'channel', contact.channel, 'result', contact.result, 'memo', contact.memo,
               'crisis_detected', contact.crisis_detected, 'next_check_date', contact.next_check_date,
               'no_action_reason', contact.no_action_reason, 'plan_id', contact.plan_id,
               'plan_version', contact.plan_version, 'checklist_version_id', contact.checklist_version_id,
               'answers', coalesce(answers.items, '[]'::jsonb)
           ) AS detail
    FROM contact_records contact
    JOIN users worker ON worker.id = contact.worker_id
    LEFT JOIN LATERAL (
        SELECT jsonb_agg(jsonb_build_object(
            'question_id', question.id, 'question_text', question.label,
            'answer_code', answer.answer_code, 'note', answer.note
        ) ORDER BY question.sort_order, question.id) AS items
        FROM contact_answers answer
        JOIN checklist_questions question ON question.id = answer.question_id
        WHERE answer.contact_id = contact.id
    ) answers ON true
    WHERE contact.client_id = %(client_id)s

    UNION ALL

    SELECT action.id, 'BENEFIT', action.occurred_at,
           jsonb_build_object('id', worker.id, 'name', worker.name),
           benefit.name || ' · ' ||
           CASE action.action
               WHEN 'DISCOVERED' THEN '혜택 발굴' WHEN 'GUIDED' THEN '안내'
               WHEN 'DOCUMENTS_PREPARED' THEN '서류 준비' WHEN 'APPLICATION_ASSISTED' THEN '신청 지원'
               WHEN 'APPLIED' THEN '신청 완료' WHEN 'RECEIPT_VERIFIED' THEN '수급 확인'
               WHEN 'DEFERRED' THEN '보류' ELSE '보류 해제' END,
           jsonb_build_object(
               'client_benefit_id', client_benefit.id, 'benefit_id', benefit.id,
               'benefit_name', benefit.name, 'benefit_version_id', action.benefit_version_id,
               'contact_id', action.contact_id, 'action', action.action,
               'before_state', action.before_state, 'after_state', action.after_state,
               'reason', action.reason
           )
    FROM benefit_actions action
    JOIN client_benefits client_benefit ON client_benefit.id = action.client_benefit_id
    JOIN benefits benefit ON benefit.id = client_benefit.benefit_id
    JOIN users worker ON worker.id = action.actor_id
    WHERE client_benefit.client_id = %(client_id)s

    UNION ALL

    SELECT referral.id, 'REFERRAL_REQUEST', referral.requested_at,
           jsonb_build_object('id', worker.id, 'name', worker.name),
           target.name || ' · 연계 요청',
           """ + _REFERRAL_DETAIL + """
    FROM referrals referral
    JOIN contact_records contact ON contact.id = referral.contact_id
    JOIN organizations target ON target.id = referral.target_organization_id
    JOIN users worker ON worker.id = referral.requested_by
    WHERE contact.client_id = %(client_id)s

    UNION ALL

    SELECT event.id, 'REFERRAL_STATUS_CHANGE', event.occurred_at,
           jsonb_build_object('id', worker.id, 'name', worker.name),
           target.name || ' · ' ||
           CASE event.to_status WHEN 'IN_PROGRESS' THEN '접수' WHEN 'COMPLETED' THEN '완료'
                                ELSE '반려' END,
           """ + _REFERRAL_DETAIL + """ || jsonb_build_object(
               'from_status', event.from_status, 'to_status', event.to_status,
               'change_reason', event.reason, 'change_result', event.result
           )
    FROM referral_events event
    JOIN referrals referral ON referral.id = event.referral_id
    JOIN contact_records contact ON contact.id = referral.contact_id
    JOIN organizations target ON target.id = referral.target_organization_id
    JOIN users worker ON worker.id = event.actor_id
    WHERE contact.client_id = %(client_id)s AND event.from_status IS NOT NULL
), filtered_events AS (
    SELECT * FROM events
    WHERE (%(event_type)s::text IS NULL OR event_type = %(event_type)s)
      AND (%(from_date)s::date IS NULL
           OR occurred_at >= (%(from_date)s::date::timestamp AT TIME ZONE 'Asia/Seoul'))
      AND (%(to_date)s::date IS NULL
           OR occurred_at < ((%(to_date)s::date + 1)::timestamp AT TIME ZONE 'Asia/Seoul'))
)
"""


def _query_time(connection: Connection, user_id: UUID) -> datetime:
    row = connection.execute(
        "SELECT transaction_timestamp() AS queried_at FROM users WHERE id = %s AND is_active",
        (user_id,),
    ).fetchone()
    if row is None:
        raise ApiError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return row["queried_at"]


def _field_info(
    value: Any,
    source_label: str | None,
    checked_at: Any,
    *,
    source_url: str | None = None,
    status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    available = value is not None
    return {
        "status": status or ("AVAILABLE" if available else "MISSING"),
        "reason": reason if reason is not None else (None if available else "자료가 없습니다."),
        "source_label": source_label,
        "source_url": source_url,
        "checked_at": checked_at,
    }


def _profile_value(profile: dict | None, field: str) -> Any:
    if profile is None:
        return None
    evidence = profile["field_evidence"].get(field, {})
    return profile.get(field) if evidence.get("status") == "KNOWN" else None


def _profile_info(profile: dict | None, field: str) -> dict[str, Any]:
    evidence = profile["field_evidence"].get(field, {}) if profile else {}
    value = _profile_value(profile, field)
    status = evidence.get("status", "MISSING")
    return _field_info(
        value, evidence.get("source_label"), evidence.get("checked_at"),
        source_url=evidence.get("source_url"),
        status="AVAILABLE" if status == "KNOWN" and value is not None else (
            "MISSING" if status == "KNOWN" else status
        ),
        reason=evidence.get("missing_reason"),
    )


def _list_item(row: dict[str, Any]) -> ClientListItem:
    snapshot = row["risk_snapshot"]
    score = snapshot["score"] if snapshot else None
    grade = snapshot["grade"] if snapshot else None
    calculated_at = snapshot["calculated_at"] if snapshot else None
    source = f"위험모델 {row['risk_model_version']}" if snapshot else None
    risk_reason = "이전 계산 결과입니다." if row["risk_stale"] else None
    missing_risk_reason = {
        "FAILED": "위험점수 계산에 실패했습니다.",
        "INCOMPLETE": "위험점수 계산에 필요한 자료가 부족합니다.",
        "RUNNING": "위험점수를 계산하고 있습니다.",
    }.get(row["calculation_status"], "위험점수 계산 결과가 없습니다.")
    field_info = {
        "age": _field_info(row["age"], row["source_label"], row["basic_info_checked_at"]),
        "household_type": _profile_info(row["profile"], "household_type"),
        "risk.score": _field_info(
            score, source, calculated_at, status="AVAILABLE" if score is not None else "UNAVAILABLE",
            reason=risk_reason if score is not None else missing_risk_reason,
        ),
        "risk.grade": _field_info(
            grade, source, calculated_at, status="AVAILABLE" if grade is not None else "UNAVAILABLE",
            reason=risk_reason if grade is not None else missing_risk_reason,
        ),
    }
    for field in ("last_success_at", "last_attempt_at"):
        field_info[field] = _field_info(
            row[field], "방문·전화 기록", row[field],
            reason=None if row[field] is not None else "해당 연락 기록이 없습니다.",
        )
    field_info["next_check_date"] = _field_info(
        row["next_check_date"], "방문·전화 기록", row["next_date_checked_at"],
        reason=None if row["next_check_date"] is not None else "연락 기록이 없습니다.",
    )
    return ClientListItem(
        id=row["id"], client_number=row["client_number"], name=row["name"], age=row["age"],
        region={"id": row["region_id"], "name": row["region_name"], "district_name": row["district_name"]},
        workers=row["workers"], household_type=_profile_value(row["profile"], "household_type"),
        risk={
            "score": score, "grade": grade, "calculation_status": row["calculation_status"],
            "completeness": snapshot["completeness"] if snapshot else None,
            "stale": row["risk_stale"], "calculated_at": calculated_at, "summary": row["risk_summary"],
        },
        last_success_at=row["last_success_at"], last_attempt_at=row["last_attempt_at"],
        next_check_date=row["next_check_date"], needs_benefit_review=row["needs_benefit_review"],
        field_info=field_info,
    )


def _client_row(connection: Connection, user_id: UUID, client_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        _CLIENT_QUERY + "SELECT * FROM client_rows WHERE id = %(client_id)s",
        {"user_id": user_id, "client_id": client_id},
    ).fetchone()
    if row is None:
        raise ApiError("CLIENT_NOT_FOUND", "대상자를 찾을 수 없습니다.", status_code=404)
    return row


def list_clients(connection: Connection, user_id: UUID, query: ClientListQuery) -> ClientListData:
    queried_at = _query_time(connection, user_id)
    conditions = []
    params: dict[str, Any] = {"user_id": user_id}
    if query.search:
        conditions.append(
            "(strpos(lower(name), lower(%(search)s)) > 0 "
            "OR strpos(lower(client_number), lower(%(search)s)) > 0)"
        )
        params["search"] = query.search
    if query.region_id is not None:
        conditions.append("region_id = %(region_id)s")
        params["region_id"] = query.region_id
    if query.risk_grade is not None:
        conditions.append("risk_snapshot ->> 'grade' = %(risk_grade)s")
        params["risk_grade"] = query.risk_grade
    if query.needs_benefit_review is not None:
        conditions.append("needs_benefit_review = %(needs_benefit_review)s")
        params["needs_benefit_review"] = query.needs_benefit_review
    if query.unchecked_days is not None:
        conditions.append(
            "(last_success_at IS NULL "
            "OR extract(epoch FROM (transaction_timestamp() - last_success_at)) / 86400 >= %(unchecked_days)s)"
        )
        params["unchecked_days"] = query.unchecked_days
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = connection.execute(
        _CLIENT_QUERY + "SELECT count(*) AS total FROM client_rows" + where, params,
    ).fetchone()["total"]
    sort_column = {
        "priority": "(risk_snapshot ->> 'score')::numeric",
        "last_success_at": "last_success_at",
        "next_check_date": "next_check_date",
    }[query.sort_by]
    direction = {"asc": "ASC", "desc": "DESC"}[query.sort_order]
    offset = (query.page - 1) * query.page_size
    rows = []
    if offset < total:
        params.update(limit=query.page_size, offset=offset)
        rows = connection.execute(
            _CLIENT_QUERY + "SELECT * FROM client_rows" + where
            + f" ORDER BY {sort_column} {direction} NULLS LAST, id ASC LIMIT %(limit)s OFFSET %(offset)s",
            params,
        ).fetchall()
    return ClientListData(
        items=[_list_item(row) for row in rows], page=query.page, page_size=query.page_size,
        total=total, queried_at=queried_at,
    )


def _masked_phone(phone: str | None) -> str | None:
    if phone is None or not phone.strip():
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("0"):
        return f"{digits[:3]}-****-{digits[-4:]}"
    if len(digits) in (9, 10) and digits.startswith("02"):
        return f"02-{'*' * (len(digits) - 6)}-{digits[-4:]}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"{digits[:3]}-***-{digits[-4:]}"
    return "*" * max(len(digits), 4)


def get_client(connection: Connection, user_id: UUID, client_id: UUID) -> ClientDetailData:
    queried_at = _query_time(connection, user_id)
    row = _client_row(connection, user_id, client_id)
    item = _list_item(row)
    profile_fields = (
        "income_band", "vulnerability_flags", "fall_history", "mobility", "health_flags",
        "cooling_vulnerable", "heating_vulnerable",
    )
    profile = None
    if row["profile"] is not None:
        profile = {"revision": row["profile"]["revision"]}
        profile.update({field: _profile_value(row["profile"], field) for field in profile_fields})
    field_info = item.field_info.copy()
    for field in ("birth_date", "address"):
        field_info[field] = _field_info(row[field], row["source_label"], row["basic_info_checked_at"])
    phone_masked = _masked_phone(row["phone"])
    field_info["phone_masked"] = _field_info(phone_masked, row["source_label"], row["basic_info_checked_at"])
    location = None
    if row["latitude"] is not None and row["longitude"] is not None:
        location = {"latitude": row["latitude"], "longitude": row["longitude"]}
    field_info["location"] = _field_info(location, row["location_source"], row["location_checked_at"])
    for field in profile_fields:
        field_info[f"profile.{field}"] = _profile_info(row["profile"], field)
    benefits = connection.execute(
        """
        SELECT state.id AS client_benefit_id, benefit.id AS benefit_id, benefit.name AS benefit_name,
               state.support_status, state.resume_status, state.receipt_status,
               state.receipt_evidence, state.receipt_verified_at,
               CASE WHEN verifier.id IS NOT NULL
                    THEN jsonb_build_object('id', verifier.id, 'name', verifier.name) END AS receipt_verified_by
        FROM client_benefits state
        JOIN benefits benefit ON benefit.id = state.benefit_id
        LEFT JOIN users verifier ON verifier.id = state.receipt_verified_by
        WHERE state.client_id = %s
        ORDER BY benefit.name, benefit.id, state.id
        """,
        (client_id,),
    ).fetchall()
    return ClientDetailData(
        **item.model_dump(exclude={"field_info"}), field_info=field_info,
        queried_at=queried_at, organization={"id": row["organization_id"], "name": row["organization_name"]},
        birth_date=row["birth_date"], address=row["address"], phone_masked=phone_masked,
        location=location, profile=profile, benefits=benefits,
    )


def get_client_history(
    connection: Connection, user_id: UUID, client_id: UUID, query: ClientHistoryQuery,
) -> ClientHistoryData:
    queried_at = _query_time(connection, user_id)
    _client_row(connection, user_id, client_id)
    params = {"client_id": client_id, "event_type": query.event_type,
              "from_date": query.from_date, "to_date": query.to_date}
    total = connection.execute(
        _HISTORY_QUERY + "SELECT count(*) AS total FROM filtered_events", params,
    ).fetchone()["total"]
    offset = (query.page - 1) * query.page_size
    items = []
    if offset < total:
        params.update(limit=query.page_size, offset=offset)
        items = connection.execute(
            _HISTORY_QUERY + """
            SELECT * FROM filtered_events
            ORDER BY occurred_at DESC, event_type ASC, event_id ASC
            LIMIT %(limit)s OFFSET %(offset)s
            """, params,
        ).fetchall()
    return ClientHistoryData(
        items=items, page=query.page, page_size=query.page_size, total=total, queried_at=queried_at,
    )
