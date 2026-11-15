"""API handler에서 사용하는 공통 JSON 응답 생성 함수입니다."""

from typing import Any

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response


def success_response(
    data: Any = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """처리 결과를 반환합니다. 204 응답은 본문 없이 반환합니다."""
    if status_code == 204:
        return Response(status_code=204, headers=headers)

    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder({"data": data, "error": None}),
        headers=headers,
    )


def paginated_response(
    items: list[Any],
    *,
    page: int,
    page_size: int,
    total: int,
) -> Response:
    """현재 페이지의 목록과 전체 개수를 반환합니다."""
    return success_response(
        {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    )


def error_response(
    code: str,
    message: str,
    *,
    status_code: int = 400,
    details: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """오류 코드·안내 문구·항목별 오류를 반환합니다."""
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            {
                "data": None,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details if details is not None else [],
                },
            }
        ),
        headers=headers,
    )
