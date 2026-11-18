"""R05 주간 큐, 오늘 방문 및 계획 검토를 서비스에 연결합니다."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from config.database import get_connection, get_write_connection
from dto.common_dto import ErrorResponse
from dto.plan_dto import PlanListQuery, PlanListResponse, PlanUpdatedResponse, PlanUpdateRequest, TodayVisitListResponse
from service import plan_query_service, plan_update_service
from util.authentication import get_current_user_id, get_current_writer_id
from util.response import success_response

router = APIRouter(
    prefix="/plans",
    tags=["Plans"],
    responses={
        401: {"model": ErrorResponse, "description": "로그인이 필요합니다."},
        422: {"model": ErrorResponse, "description": "입력 내용을 확인해주세요."},
        503: {"model": ErrorResponse, "description": "DB 연결 실패"},
    },
)

CurrentUser = Annotated[UUID, Depends(get_current_user_id)]
CurrentWriter = Annotated[UUID, Depends(get_current_writer_id)]
Database = Annotated[Connection, Depends(get_connection)]
WriteDatabase = Annotated[Connection, Depends(get_write_connection, scope="function")]


@router.get(
    "",
    response_model=PlanListResponse,
    operation_id="listPlans",
    summary="주간 우선 확인 큐 조회",
    description="선택 주간의 추천 배치에서 현재 담당자가 볼 수 있는 계획을 저장된 추천 순서로 조회합니다.",
)
def list_plans(user_id: CurrentUser, connection: Database, query: Annotated[PlanListQuery, Query()]):
    return success_response(plan_query_service.list_plans(connection, user_id, query))


@router.get(
    "/today",
    response_model=TodayVisitListResponse,
    operation_id="listTodayVisits",
    summary="오늘 확정 방문 순서 조회",
    description="한국 날짜 기준 오늘 확정된 방문을 예정시각 순으로 조회합니다.",
)
def list_today_visits(user_id: CurrentUser, connection: Database):
    return success_response(plan_query_service.list_today_visits(connection, user_id))


@router.post(
    "/{plan_id}/review",
    response_model=PlanUpdatedResponse,
    operation_id="updatePlan",
    summary="계획 확정, 보류 또는 변경",
    description="확정에는 방식과 예정일시, 보류 또는 추천 방식 변경에는 사유가 필요합니다.",
    responses={
        404: {"model": ErrorResponse, "description": "계획이 없거나 권한이 없습니다."},
        409: {"model": ErrorResponse, "description": "버전, 요청 키 또는 계획 상태가 충돌합니다."},
    },
)
def update_plan(plan_id: UUID, payload: PlanUpdateRequest, user_id: CurrentWriter, connection: WriteDatabase):
    return success_response(plan_update_service.update_plan(connection, user_id, plan_id, payload))
