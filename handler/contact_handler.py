"""R07 현장 기록 요청을 서비스에 연결합니다."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from psycopg import Connection

from config.database import get_connection, get_write_connection
from dto.common_dto import ErrorResponse
from dto.contact_dto import ContactCreatedResponse, ContactCreateRequest, ContactFormResponse
from service import contact_service, contact_write_service
from util.authentication import get_contact_writer_id, get_current_user_id
from util.response import success_response

router = APIRouter(
    prefix="/clients",
    tags=["Contacts"],
    responses={
        401: {"model": ErrorResponse, "description": "로그인이 필요합니다."},
        404: {"model": ErrorResponse, "description": "대상자가 없거나 권한이 없습니다."},
        422: {"model": ErrorResponse, "description": "입력 내용을 확인해주세요."},
        503: {"model": ErrorResponse, "description": "DB 또는 체크리스트를 사용할 수 없습니다."},
    },
)

CurrentUser = Annotated[UUID, Depends(get_current_user_id)]
ContactWriter = Annotated[UUID, Depends(get_contact_writer_id)]
Database = Annotated[Connection, Depends(get_connection)]
WriteDatabase = Annotated[Connection, Depends(get_write_connection, scope="function")]


@router.get(
    "/{client_id}/contact-form",
    response_model=ContactFormResponse,
    operation_id="getContactForm",
    summary="현장 기록 양식 조회",
)
def get_contact_form(client_id: UUID, user_id: CurrentUser, connection: Database):
    return success_response(contact_service.get_contact_form(connection, user_id, client_id))


@router.post(
    "/{client_id}/contacts",
    response_model=ContactCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createContact",
    summary="현장 기록 저장",
    responses={
        409: {"model": ErrorResponse, "description": "대상자 버전, 양식 버전 또는 요청 키가 충돌합니다."},
    },
)
def create_contact(
    client_id: UUID,
    payload: ContactCreateRequest,
    user_id: ContactWriter,
    connection: WriteDatabase,
):
    saved = contact_write_service.create_contact(connection, user_id, client_id, payload)
    return success_response(saved, status_code=status.HTTP_201_CREATED)
