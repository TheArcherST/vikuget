from __future__ import annotations

import json
from typing import Any

from fastapi.responses import HTMLResponse

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


def result_response(
    *,
    body: dict[str, Any],
    status_code: int,
    extra_headers: dict[str, str] | None = None,
) -> HTMLResponse:
    headers = {**NO_STORE_HEADERS, **(extra_headers or {})}
    return HTMLResponse(
        status_code=status_code,
        content=html_document(body),
        headers=headers,
    )


def error_response(*, status_code: int, action: str, code: str, message: str) -> HTMLResponse:
    return result_response(
        status_code=status_code,
        body=error_body(action=action, code=code, message=message),
    )


def html_document(body: dict[str, Any]) -> str:
    """Embed exact JSON in a deliberately minimal HTML response.

    Escaping `<`, `>` and `&` keeps arbitrary task text from terminating the script
    element. The element's text is still valid JSON without HTML entity decoding.
    """

    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    payload = (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head><body>'
        f'<script id="vikuget-result" type="application/json">{payload}</script>'
        "</body></html>"
    )
