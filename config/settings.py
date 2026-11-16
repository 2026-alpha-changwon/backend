"""환경변수와 로컬 .env의 서버 설정을 읽습니다."""

import os
from functools import lru_cache
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    database_url: str = ""
    test_database_url: str = ""
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = Field(default_factory=list)
    session_cookie_name: str = Field(default="session", min_length=1)
    dev_user_id: UUID | None = None


@lru_cache
def get_settings() -> Settings:
    values = {**dotenv_values(ROOT_DIR / ".env"), **os.environ}
    return Settings(
        database_url=values.get("DATABASE_URL") or "",
        test_database_url=values.get("TEST_DATABASE_URL") or "",
        api_port=values.get("API_PORT") or 8000,
        cors_origins=[
            origin.strip()
            for origin in (values.get("CORS_ORIGINS") or "").split(",")
            if origin.strip()
        ],
        session_cookie_name=values.get("SESSION_COOKIE_NAME") or "session",
        dev_user_id=values.get("DEV_USER_ID") or None,
    )
