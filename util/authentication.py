"""세션 쿠키를 읽어 handler에 현재 사용자 ID를 제공합니다."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Security
from fastapi.security import APIKeyCookie
from psycopg import Connection

from config.database import get_connection
from config.settings import get_settings
from service.auth_service import get_session_user_id
from util.errors import ApiError

session_cookie = APIKeyCookie(
    name=get_settings().session_cookie_name, scheme_name="CookieAuth", auto_error=False,
)


def get_session_token(token: Annotated[str | None, Security(session_cookie)]) -> str:
    if not token:
        raise ApiError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return token


def get_current_user_id(
    token: Annotated[str, Depends(get_session_token)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> UUID:
    return get_session_user_id(connection, token)
