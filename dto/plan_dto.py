"""R05 주간 큐, 계획 검토 및 오늘 방문 목록의 요청과 응답 형식입니다."""

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from dto.client_dto import LocationInfo
from dto.common_dto import PageData, PaginationQuery, SuccessResponse

PlanChannel = Literal["VISIT", "PHONE", "MONITOR"]
ReviewStatus = Literal["RECOMMENDED", "CONFIRMED", "DEFERRED"]
ExecutionStatus = Literal["NOT_STARTED", "CONFIRMED", "CONTACT_FAILED"]
PlanUpdateAction = Literal["CONFIRM", "DEFER", "RESCHEDULE"]
PlanReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\x00]*$")]
PlanRequestKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[^\x00]*$"),
]


class PlanListQuery(PaginationQuery):
    week_start: date = Field(description="조회할 배치의 주간 시작일")
    region_id: UUID | None = Field(default=None, description="행정동 ID")
    channel: PlanChannel | None = Field(default=None, description="현재 계획의 연락 방식")
    review_status: ReviewStatus | None = None
    execution_status: ExecutionStatus | None = None


class PlanBatchInfo(BaseModel):
    id: UUID
    as_of_at: AwareDatetime
    generation: int = Field(ge=1)


class PlanClientInfo(BaseModel):
    client_id: UUID
    client_number: str
    client_name: str
    region_id: UUID
    region_name: str


class PlanUpdatedData(BaseModel):
    plan_id: UUID
    version: int = Field(ge=1)
    channel: PlanChannel
    review_status: ReviewStatus
    execution_status: ExecutionStatus
    scheduled_at: AwareDatetime | None
    defer_reason: str | None
    channel_change_reason: str | None


class PlanListItem(PlanUpdatedData, PlanClientInfo):
    recommended_channel: PlanChannel
    rank: int | None = Field(ge=1, description="저장된 추천 순위")
    score: float | None = Field(description="계획에 연결된 위험점수 스냅샷의 점수")
    recommendation_reason: str
    last_success_at: AwareDatetime | None


class PlanListData(PageData[PlanListItem]):
    week_start: date
    queried_at: AwareDatetime
    batch: PlanBatchInfo | None


class TodayVisitItem(PlanClientInfo):
    plan_id: UUID
    version: int = Field(ge=1)
    scheduled_at: AwareDatetime
    execution_status: ExecutionStatus
    location: LocationInfo | None
    order: int = Field(ge=1, description="오늘 방문 목록 내의 순서")


class TodayVisitListData(BaseModel):
    today_date: date
    queried_at: AwareDatetime
    items: list[TodayVisitItem]


class PlanUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: PlanRequestKey = Field(description="같은 변경을 재시도할 때 유지하는 요청 키")
    expected_version: int = Field(ge=1, le=2**63 - 1, strict=True)
    action: PlanUpdateAction
    channel: Literal["VISIT", "PHONE"] | None = None
    scheduled_at: AwareDatetime | None = Field(default=None, description="시간대를 포함한 예정 일시")
    reason: PlanReason | None = None

    @field_validator("scheduled_at", mode="before")
    @classmethod
    def validate_schedule_datetime(cls, value: object) -> object:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str) and re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ]", value):
            return value
        raise ValueError("scheduled_at은 시간대를 포함한 ISO 8601 일시여야 합니다.")

    @field_validator("scheduled_at")
    @classmethod
    def normalize_schedule_datetime(cls, value: datetime | None) -> datetime | None:
        try:
            return value.astimezone(timezone.utc) if value is not None else None
        except OverflowError as error:
            raise ValueError("scheduled_at이 처리 가능한 일시 범위를 벗어났습니다.") from error


PlanListResponse = SuccessResponse[PlanListData]
TodayVisitListResponse = SuccessResponse[TodayVisitListData]
PlanUpdatedResponse = SuccessResponse[PlanUpdatedData]
