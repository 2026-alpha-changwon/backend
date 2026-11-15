"""한 요청에서 사용하는 PostgreSQL 읽기 연결입니다."""

from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from config.settings import get_settings
from util.errors import ApiError


def get_connection() -> Iterator[psycopg.Connection]:
    database_url = get_settings().database_url
    if not database_url:
        raise ApiError("SERVICE_UNAVAILABLE", "DB 연결 설정이 필요합니다.", status_code=503)

    try:
        with psycopg.connect(
            database_url,
            row_factory=dict_row,
            autocommit=True,
            connect_timeout=5,
        ) as connection:
            connection.execute("SET TIME ZONE 'Asia/Seoul'")
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                yield connection
    except (psycopg.OperationalError, psycopg.InterfaceError) as error:
        raise ApiError(
            "SERVICE_UNAVAILABLE", "DB에 연결할 수 없습니다. 잠시 후 다시 시도해주세요.", status_code=503,
        ) from error
