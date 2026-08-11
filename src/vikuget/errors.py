from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, max-age=0, private",
    "Pragma": "no-cache",
    "Expires": "0",
}


class ApiProblem(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


def error_body(*, action: str, code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "error": {"code": code, "message": message},
    }


def error_response(*, status_code: int, action: str, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_body(action=action, code=code, message=message),
        headers=NO_STORE_HEADERS,
    )
