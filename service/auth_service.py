"""기존 세션을 현재 사용자에게 연결합니다."""

from hashlib import sha256
from uuid import UUID

from psycopg import Connection

from util.errors import ApiError


def get_session_user_id(connection: Connection, token: str) -> UUID:
    session = connection.execute(
        """
        SELECT session.user_id
        FROM user_sessions AS session
        JOIN users AS account ON account.id = session.user_id
        WHERE session.token_hash = %s
          AND session.revoked_at IS NULL
          AND session.expires_at > transaction_timestamp()
          AND account.is_active
        """,
        (sha256(token.encode()).hexdigest(),),
    ).fetchone()
    if session is None:
        raise ApiError("AUTH_REQUIRED", "로그인이 필요합니다.", status_code=401)
    return session["user_id"]
