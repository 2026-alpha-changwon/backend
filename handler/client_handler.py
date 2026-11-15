"""R03의 HTTP 요청을 받고 서비스 결과를 공통 형식으로 반환합니다."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from psycopg import Connection

from config.database import get_connection
from dto.client_dto import (
    ClientDetailResponse,
    ClientHistoryQuery,
    ClientHistoryResponse,
    ClientListQuery,
    ClientListResponse,
)
from dto.common_dto import ErrorResponse
from service import client_service
from util.authentication import get_current_user_id
from util.response import success_response

router = APIRouter(
    prefix="/clients",
    tags=["Clients"],
    responses={
        401: {"model": ErrorResponse, "description": "로그인이 필요합니다."},
        422: {"model": ErrorResponse, "description": "입력 내용을 확인해주세요."},
        503: {"model": ErrorResponse, "description": "DB 연결 실패"},
    },
)
CurrentUser = Annotated[UUID, Depends(get_current_user_id)]
Database = Annotated[Connection, Depends(get_connection)]


@router.get("", response_model=ClientListResponse, operation_id="listClients", summary="대상자 목록 조회")
def list_clients(user_id: CurrentUser, connection: Database, query: Annotated[ClientListQuery, Query()]):
    return success_response(client_service.list_clients(connection, user_id, query))


@router.get(
    "/{client_id}",
    response_model=ClientDetailResponse,
    operation_id="getClient",
    summary="대상자 상세 조회",
    responses={404: {"model": ErrorResponse, "description": "대상자가 없거나 열람 권한이 없습니다."}},
)
def get_client(client_id: UUID, user_id: CurrentUser, connection: Database):
    return success_response(client_service.get_client(connection, user_id, client_id))


@router.get(
    "/{client_id}/history",
    response_model=ClientHistoryResponse,
    operation_id="getClientHistory",
    summary="대상자 통합 이력 조회",
    responses={404: {"model": ErrorResponse, "description": "대상자가 없거나 열람 권한이 없습니다."}},
)
def get_client_history(
    client_id: UUID,
    user_id: CurrentUser,
    connection: Database,
    query: Annotated[ClientHistoryQuery, Query()],
):
    return success_response(client_service.get_client_history(connection, user_id, client_id, query))
