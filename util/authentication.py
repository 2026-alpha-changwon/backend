"""개발용 담당자 또는 세션 쿠키로 현재 사용자 ID를 제공합니다."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Security
from fastapi.security import APIKeyCookie
from psycopg import Connection

from config.database import get_connection, get_write_connection
from config.settings import get_settings
from service.auth_service import get_session_user_id
from util.errors import ApiError

session_cookie = APIKeyCookie(
    name=get_settings().session_cookie_name, scheme_name="CookieAuth", auto_error=False,
)


def get_session_token(token: Annotated[str | None, Security(session_cookie)]) -> str:
    if not token and get_settings().dev_user_id is None:
        raise ApiError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return token or ""


def get_current_user_id(
    token: Annotated[str, Depends(get_session_token)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> UUID:
    dev_user_id = get_settings().dev_user_id
    if dev_user_id is not None:
        return dev_user_id
    return get_session_user_id(connection, token)


def _get_write_credentials(token: Annotated[str | None, Security(session_cookie)]) -> str | UUID:
    if token:
        return token
    dev_writer_id = get_settings().contact_write_dev_user_id
    if dev_writer_id is None:
        raise ApiError("AUTH_REQUIRED", "저장하려면 로그인이 필요합니다.", status_code=401)
    return dev_writer_id


def get_current_writer_id(
    credentials: Annotated[str | UUID, Depends(_get_write_credentials)],
    connection: Annotated[Connection, Depends(get_write_connection, scope="function")],
) -> UUID:
    if isinstance(credentials, UUID):
        return credentials
    return get_session_user_id(connection, credentials)
