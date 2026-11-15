"""util.response에서 반환하는 공통 응답과 페이지 형식입니다."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class SuccessResponse(BaseModel, Generic[T]):
    data: T
    error: None


class ErrorDetail(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail]


class ErrorResponse(BaseModel):
    data: None
    error: ErrorBody


class PaginationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1, description="1부터 시작하는 페이지 번호")
    page_size: int = Field(default=20, ge=1, le=100, description="페이지당 항목 수")


class PageData(BaseModel, Generic[T]):
    items: list[T]
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    total: int = Field(ge=0, description="권한과 모든 필터를 적용한 전체 항목 수")
