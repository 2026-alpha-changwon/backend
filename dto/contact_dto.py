"""R07 현장 기록 양식 조회와 저장의 요청·응답 형식입니다."""

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StringConstraints,
    field_validator,
    model_serializer,
    model_validator,
)

from dto.common_dto import SuccessResponse

AnswerCode = Literal["RISK_FOUND", "NO_RISK", "UNCONFIRMED", "NOT_APPLICABLE"]
ContactChannel = Literal["VISIT", "PHONE"]
ContactResult = Literal["SUCCESS", "FAILED", "REFUSED"]
DatabaseText = Annotated[str, StringConstraints(pattern=r"^[^\x00]*$")]
RequestKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[^\x00]*$"),
]


class ContactQuestion(BaseModel):
    question_id: UUID
    question_code: str
    label: str
    sort_order: int = Field(ge=0)
    is_required: bool
    allowed_answers: list[AnswerCode]


class ContactFormData(BaseModel):
    client_id: UUID
    client_version: int = Field(ge=1, description="저장할 때 expected_version에 넣는 대상자 버전")
    checklist_version_id: UUID
    version_code: str
    name: str
    questions: list[ContactQuestion]


class ContactAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: UUID
    answer_code: AnswerCode
    note: DatabaseText | None = None


class ContactCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: RequestKey = Field(description="같은 저장 요청을 재시도할 때 그대로 사용하는 키")
    expected_version: int = Field(ge=1, le=2**63 - 1, strict=True)
    checklist_version_id: UUID
    answers: list[ContactAnswerInput] = Field(default_factory=list)
    channel: ContactChannel
    occurred_at: AwareDatetime = Field(description="시간대가 포함된 실제 방문 또는 전화 일시")
    result: ContactResult
    crisis_detected: StrictBool | None = Field(default=None, description="미확인인 경우 null")
    memo: DatabaseText | None = Field(default=None, max_length=2_000)
    next_check_date: date = Field(description="성공·실패·거부 모두 필수인 다음 확인 예정일")
    no_action_reason: DatabaseText | None = None
    plan_id: UUID | None = Field(default=None, description="연결할 확정 계획 ID")
    plan_version: int | None = Field(
        default=None, ge=1, le=2**63 - 1, strict=True, description="변경 충돌을 확인할 현재 계획의 version",
    )

    @model_validator(mode="after")
    def validate_plan_link(self) -> "ContactCreateRequest":
        if (self.plan_id is None) != (self.plan_version is None):
            raise ValueError("plan_id와 plan_version을 함께 입력해 주세요.")
        return self

    @field_validator("occurred_at", mode="before")
    @classmethod
    def validate_contact_datetime(cls, value: object) -> object:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str) and re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ]", value):
            return value
        raise ValueError("occurred_at은 시간대를 포함한 ISO 8601 일시여야 합니다.")

    @field_validator("occurred_at")
    @classmethod
    def normalize_contact_datetime(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(timezone.utc)
        except OverflowError as error:
            raise ValueError("occurred_at이 처리 가능한 일시 범위를 벗어났습니다.") from error

    @field_validator("next_check_date", mode="before")
    @classmethod
    def validate_check_date(cls, value: object) -> object:
        if type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return value
        raise ValueError("next_check_date는 YYYY-MM-DD 형식의 날짜여야 합니다.")


class ContactPlanData(BaseModel):
    plan_id: UUID
    plan_version: int = Field(ge=1, description="현장 기록 저장 후의 계획 버전")
    review_status: Literal["CONFIRMED"]
    execution_status: Literal["CONFIRMED", "CONTACT_FAILED"]


class ContactCreatedData(BaseModel):
    contact_id: UUID
    client_id: UUID
    client_version: int = Field(ge=1)
    last_success_at: AwareDatetime | None
    last_attempt_at: AwareDatetime | None
    next_check_date: date
    calculation_status: Literal["PENDING"] = "PENDING"
    plan: ContactPlanData | None = Field(default=None, description="계획에 연결한 경우에만 반환")

    @model_serializer(mode="wrap")
    def omit_unlinked_plan(self, handler: SerializerFunctionWrapHandler):
        data = handler(self)
        if self.plan is None:
            data.pop("plan", None)
        return data


ContactFormResponse = SuccessResponse[ContactFormData]
ContactCreatedResponse = SuccessResponse[ContactCreatedData]
