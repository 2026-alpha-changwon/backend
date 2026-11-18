"""먼저가요 창원 API 실행 진입점입니다."""

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from config.settings import get_settings
from handler.client_handler import router as client_router
from handler.contact_handler import router as contact_router
from handler.plan_handler import router as plan_router
from util.errors import ApiError
from util.response import error_response

settings = get_settings()
app = FastAPI(
    title="먼저가요 창원 API",
    version="0.2.0",
    description=(
        "개발용 공통 담당자로 조회합니다. 쿠키 없이 Try it out과 Execute로 조회 API를 실행할 수 있습니다."
        if settings.dev_user_id is not None
        else "유효한 세션 쿠키로 현재 사용자를 확인합니다."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(client_router)
app.include_router(contact_router)
app.include_router(plan_router)


@app.exception_handler(ApiError)
async def handle_api_error(request, error: ApiError):
    return error_response(error.code, error.message, status_code=error.status_code)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request, error: RequestValidationError):
    details = [
        {"field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]}
        for item in error.errors()
    ]
    return error_response(
        "INVALID_REQUEST", "입력 내용을 확인해주세요.", status_code=422, details=details,
    )


@app.exception_handler(HTTPException)
async def handle_http_error(request, error: HTTPException):
    return error_response(
        "HTTP_ERROR", str(error.detail), status_code=error.status_code, headers=error.headers,
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request, error: Exception):
    logging.getLogger(__name__).exception("API 처리 중 오류가 발생했습니다.")
    return error_response("INTERNAL_SERVER_ERROR", "요청을 처리하지 못했습니다.", status_code=500)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.api_port)
