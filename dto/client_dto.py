"""R03 대상자 목록·상세·통합 이력의 요청과 응답 형식입니다."""

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from dto.common_dto import PageData, PaginationQuery, SuccessResponse

HouseholdType = Literal["SINGLE", "ELDERLY_COUPLE", "WITH_FAMILY"]
SupportStatus = Literal[
    "NEEDS_REVIEW", "GUIDED", "DOCUMENTS_PREPARING",
    "APPLICATION_ASSISTED", "APPLIED", "DEFERRED",
]
ReceiptStatus = Literal["UNKNOWN", "CONFIRMED", "NOT_RECEIVING"]
ReferralStatus = Literal["REQUESTED", "IN_PROGRESS", "COMPLETED", "REJECTED"]
HistoryEventType = Literal[
    "CONTACT", "BENEFIT", "REFERRAL_REQUEST", "REFERRAL_STATUS_CHANGE",
]


class ClientListQuery(PaginationQuery):
    search: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, description="이름 또는 가상 번호의 부분 검색. 빈 문자열이면 전체 조회",
    )
    region_id: UUID | None = Field(default=None, description="행정동 ID")
    risk_grade: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1)
    ] | None = Field(default=None, description="위험모델의 등급 코드. 등급 경계는 모델 정책을 따름")
    needs_benefit_review: bool | None = Field(
        default=None, description="NEEDS_REVIEW 혜택 존재 여부. 생략하면 양쪽 모두 조회",
    )
    unchecked_days: int | None = Field(
        default=None, ge=1,
        description="마지막 성공 확인이 조회 시각에서 N일 이전이거나 성공 이력이 없는 대상자",
    )
    sort_by: Literal["priority", "last_success_at", "next_check_date"] = Field(
        default="priority", description="priority는 현재 표시하는 위험점수 기준",
    )
    sort_order: Literal["asc", "desc"] = "desc"


class ClientHistoryQuery(PaginationQuery):
    event_type: HistoryEventType | None = None
    from_date: date | None = Field(default=None, description="시작 한국 날짜. 해당 날짜 포함")
    to_date: date | None = Field(default=None, description="종료 한국 날짜. 해당 날짜 포함")

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("to_date는 from_date와 같거나 이후여야 합니다.")
        return self


class WorkerInfo(BaseModel):
    id: UUID
    name: str


class OrganizationInfo(BaseModel):
    id: UUID
    name: str


class RegionInfo(BaseModel):
    id: UUID
    name: str
    district_name: str


class FieldInfo(BaseModel):
    """키는 age, profile.health_flags 등 응답 데이터 내부의 필드 경로입니다."""

    status: Literal[
        "AVAILABLE", "MISSING", "UNCONFIRMED", "UNAVAILABLE", "RESTRICTED", "NOT_APPLICABLE",
    ]
    reason: str | None
    source_label: str | None
    source_url: str | None
    checked_at: AwareDatetime | None


class RiskSummary(BaseModel):
    score: float | None
    grade: str | None
    calculation_status: Literal["PENDING", "RUNNING", "COMPLETE", "INCOMPLETE", "FAILED"]
    completeness: Literal["COMPLETE", "INCOMPLETE"] | None
    stale: bool = Field(description="표시한 결과가 최신 입력·모델·실행과 맞지 않으면 true")
    calculated_at: AwareDatetime | None
    summary: list[str] = Field(description="표시한 위험결과의 근거 요약. 계산 결과가 없으면 빈 배열")


class ClientListItem(BaseModel):
    id: UUID
    client_number: str
    name: str
    age: int | None = Field(ge=0, description="조회 한국 날짜 기준 만 나이")
    region: RegionInfo
    workers: list[WorkerInfo] = Field(description="조회 시각에 유효한 배정 담당자. ID 오름차순")
    household_type: HouseholdType | None
    risk: RiskSummary
    last_success_at: AwareDatetime | None
    last_attempt_at: AwareDatetime | None
    next_check_date: date | None = Field(description="최신 연락의 다음 확인일. 연락 기록이 없으면 null")
    needs_benefit_review: bool
    field_info: dict[str, FieldInfo] = Field(description="자료 상태·출처·확인일. 상세에서 같은 규칙 사용")


class ClientListData(PageData[ClientListItem]):
    queried_at: AwareDatetime
    data_mode: Literal["DEMO"] = "DEMO"


class LocationInfo(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ClientProfile(BaseModel):
    """최신 revision 전체를 사용하며 과거 revision의 값으로 보충하지 않습니다."""

    revision: int = Field(ge=1)
    income_band: Literal["LOW_INCOME", "GENERAL"] | None
    vulnerability_flags: list[Literal[
        "LIVING_ALONE", "LOW_INCOME", "LIMITED_SUPPORT", "HOUSING_ISSUE",
    ]] | None
    fall_history: bool | None
    mobility: Literal["INDEPENDENT", "WALKING_AID", "NEEDS_ASSISTANCE"] | None
    health_flags: list[Literal[
        "HYPERTENSION", "DIABETES", "ARTHRITIS", "HEART_DISEASE",
    ]] | None
    cooling_vulnerable: bool | None
    heating_vulnerable: bool | None


class BenefitState(BaseModel):
    support_status: SupportStatus
    resume_status: SupportStatus | None
    receipt_status: ReceiptStatus
    receipt_evidence: str | None
    receipt_verified_at: AwareDatetime | None


class ClientBenefit(BenefitState):
    client_benefit_id: UUID
    benefit_id: UUID
    benefit_name: str
    receipt_verified_by: WorkerInfo | None


class ClientDetailData(ClientListItem):
    queried_at: AwareDatetime
    data_mode: Literal["DEMO"] = "DEMO"
    organization: OrganizationInfo
    birth_date: date | None
    address: str | None
    phone_masked: str | None
    location: LocationInfo | None
    profile: ClientProfile | None
    benefits: list[ClientBenefit]


class ContactAnswer(BaseModel):
    question_id: UUID
    question_text: str
    answer_code: Literal["RISK_FOUND", "NO_RISK", "UNCONFIRMED", "NOT_APPLICABLE"]
    note: str | None


class ContactHistoryDetail(BaseModel):
    channel: Literal["VISIT", "PHONE"]
    result: Literal["SUCCESS", "FAILED", "REFUSED"]
    memo: str | None
    crisis_detected: bool | None
    next_check_date: date = Field(description="성공·실패·거부 모두 필수")
    no_action_reason: str | None
    plan_id: UUID | None
    plan_version: int | None = Field(ge=1)
    checklist_version_id: UUID
    answers: list[ContactAnswer]


class BenefitHistoryDetail(BaseModel):
    client_benefit_id: UUID
    benefit_id: UUID
    benefit_name: str
    benefit_version_id: UUID
    contact_id: UUID | None
    action: Literal[
        "DISCOVERED", "GUIDED", "DOCUMENTS_PREPARED", "APPLICATION_ASSISTED",
        "APPLIED", "RECEIPT_VERIFIED", "DEFERRED", "RESUMED",
    ]
    before_state: BenefitState
    after_state: BenefitState
    reason: str | None


class ReferralHistoryDetail(BaseModel):
    referral_id: UUID
    contact_id: UUID
    target_organization: OrganizationInfo
    request_reason: str
    requested_action: str
    due_date: date | None
    current_status: ReferralStatus = Field(description="조회 시각의 현재 상태")
    accepted_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    rejected_at: AwareDatetime | None
    current_result: str | None = Field(description="조회 시각의 처리 결과 또는 반려 사유")
    delivery_status: Literal["NOT_SENT"] = "NOT_SENT"


class ReferralStatusHistoryDetail(ReferralHistoryDetail):
    from_status: ReferralStatus
    to_status: ReferralStatus
    change_reason: str | None
    change_result: str | None


class HistoryEvent(BaseModel):
    event_id: UUID
    occurred_at: AwareDatetime
    worker: WorkerInfo
    summary: str


class ContactHistoryEvent(HistoryEvent):
    event_type: Literal["CONTACT"]
    detail: ContactHistoryDetail


class BenefitHistoryEvent(HistoryEvent):
    event_type: Literal["BENEFIT"]
    detail: BenefitHistoryDetail


class ReferralRequestHistoryEvent(HistoryEvent):
    event_type: Literal["REFERRAL_REQUEST"]
    detail: ReferralHistoryDetail


class ReferralStatusHistoryEvent(HistoryEvent):
    event_type: Literal["REFERRAL_STATUS_CHANGE"]
    detail: ReferralStatusHistoryDetail


ClientHistoryItem = Annotated[
    ContactHistoryEvent | BenefitHistoryEvent | ReferralRequestHistoryEvent | ReferralStatusHistoryEvent,
    Field(discriminator="event_type"),
]


class ClientHistoryData(PageData[ClientHistoryItem]):
    queried_at: AwareDatetime
    data_mode: Literal["DEMO"] = "DEMO"


class ClientListResponse(SuccessResponse[ClientListData]):
    """대상자 목록 응답입니다."""


class ClientDetailResponse(SuccessResponse[ClientDetailData]):
    """대상자 상세 응답입니다."""


class ClientHistoryResponse(SuccessResponse[ClientHistoryData]):
    """대상자 통합 이력 응답입니다."""
