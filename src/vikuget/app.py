from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .client import VikunjaClient
from .config import Settings
from .errors import NO_STORE_HEADERS, ApiProblem, error_body, error_response
from .replays import CachedResponse, RequestReplayStore
from .views import comment_view, fingerprint, nonblank, task_view

logger = logging.getLogger(__name__)

API_PREFIX = "/v1/{access_token}"


@dataclass
class Services:
    settings: Settings
    vikunja: VikunjaClient
    replays: RequestReplayStore


async def get_services(request: Request) -> Services:
    return request.app.state.services


async def require_access_token(
    request: Request,
    access_token: Annotated[str, Path(min_length=32, max_length=512)],
) -> None:
    if "access_token" in request.query_params:
        raise ApiProblem(
            status.HTTP_400_BAD_REQUEST,
            "query_token_forbidden",
            "ACCESS_TOKEN belongs only in the URL path.",
        )
    expected = request.app.state.services.settings.access_token.get_secret_value()
    if not hmac.compare_digest(access_token, expected):
        raise ApiProblem(
            status.HTTP_401_UNAUTHORIZED, "authentication_failed", "Authentication failed."
        )


ServicesDep = Annotated[Services, Depends(get_services)]
RequestTag = Annotated[str, Query(min_length=1, max_length=1024)]
TaskId = Annotated[int, Path(ge=1)]


def json_response(body: dict[str, Any], *, replayed: bool = False) -> JSONResponse:
    headers = dict(NO_STORE_HEADERS)
    if replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(status_code=status.HTTP_200_OK, content=body, headers=headers)


async def run_mutation(
    services: Services,
    *,
    action: str,
    request_tag: str,
    arguments: Mapping[str, Any],
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    cached = services.replays.claim(
        request_tag=request_tag,
        fingerprint=fingerprint(action, arguments),
    )
    if cached is not None:
        return JSONResponse(
            status_code=cached.status_code,
            content=cached.body,
            headers={**NO_STORE_HEADERS, "Idempotent-Replay": "true"},
        )

    try:
        response = CachedResponse(
            status.HTTP_200_OK,
            {"ok": True, "action": action, **await operation()},
        )
    except ApiProblem as problem:
        response = CachedResponse(
            problem.status_code,
            error_body(action=action, code=problem.code, message=problem.message),
        )
    except Exception:
        logger.exception("Unhandled mutation failure for action %s", action)
        response = CachedResponse(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_body(
                action=action,
                code="internal_error",
                message="The action could not be completed safely.",
            ),
        )

    services.replays.complete(request_tag=request_tag, response=response)
    return JSONResponse(
        status_code=response.status_code,
        content=response.body,
        headers=NO_STORE_HEADERS,
    )


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    configured_settings = settings or Settings()
    services = Services(
        settings=configured_settings,
        vikunja=VikunjaClient(configured_settings, transport),
        replays=RequestReplayStore(
            configured_settings.request_store_path,
            configured_settings.request_retention_days,
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services.replays.open()
        await services.vikunja.open()
        app.state.services = services
        try:
            yield
        finally:
            await services.vikunja.close()
            services.replays.close()

    app = FastAPI(
        title="vikuget",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def allow_only_https_get(
        request: Request, call_next: Callable[..., Awaitable[Any]]
    ) -> Any:
        if request.method != "GET":
            return error_response(
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                action="request_rejected",
                code="method_not_allowed",
                message="Only GET requests are accepted.",
            )
        # Docker calls /health directly; all other requests must come from Traefik.
        if request.url.path != "/health" and request.headers.get("x-forwarded-proto") != "https":
            return error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                action="request_rejected",
                code="https_required",
                message="HTTPS is required.",
            )
        return await call_next(request)

    @app.exception_handler(ApiProblem)
    async def handle_api_problem(_: Request, problem: ApiProblem) -> JSONResponse:
        return error_response(
            status_code=problem.status_code,
            action="request_rejected",
            code=problem.code,
            message=problem.message,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return error_response(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            action="request_rejected",
            code="invalid_request",
            message="Request parameters are invalid.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, problem: StarletteHTTPException) -> JSONResponse:
        code = (
            "not_found" if problem.status_code == status.HTTP_404_NOT_FOUND else "request_rejected"
        )
        message = "Endpoint not found." if code == "not_found" else "Request rejected."
        return error_response(
            status_code=problem.status_code,
            action="request_rejected",
            code=code,
            message=message,
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/tasks", dependencies=[Depends(require_access_token)])
    async def list_tasks(
        services: ServicesDep,
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        tasks, total = await services.vikunja.list_tasks(page=page, per_page=per_page)
        result: dict[str, Any] = {
            "ok": True,
            "action": "tasks_listed",
            "tasks": [task_view(task) for task in tasks],
            "pagination": {"page": page, "per_page": per_page, "count": len(tasks)},
        }
        if total is not None:
            result["pagination"]["total"] = total
        return json_response(result)

    @app.get(f"{API_PREFIX}/tasks/search", dependencies=[Depends(require_access_token)])
    async def search_tasks(
        services: ServicesDep,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> JSONResponse:
        query = nonblank(q, field_name="q")
        tasks, total = await services.vikunja.list_tasks(page=page, per_page=per_page, query=query)
        result: dict[str, Any] = {
            "ok": True,
            "action": "tasks_searched",
            "tasks": [task_view(task) for task in tasks],
            "pagination": {"page": page, "per_page": per_page, "count": len(tasks)},
        }
        if total is not None:
            result["pagination"]["total"] = total
        return json_response(result)

    @app.get(f"{API_PREFIX}/tasks/create", dependencies=[Depends(require_access_token)])
    async def create_task(
        services: ServicesDep,
        request_tag: RequestTag,
        title: Annotated[str, Query(min_length=1, max_length=1024)],
        description: Annotated[str | None, Query(max_length=20_000)] = None,
        due_date: Annotated[date | None, Query()] = None,
    ) -> JSONResponse:
        clean_title = nonblank(title, field_name="title")

        async def operation() -> dict[str, Any]:
            task = await services.vikunja.create_task(
                title=clean_title,
                description=description,
                due_date=due_date,
            )
            return {"task": task_view(task)}

        return await run_mutation(
            services,
            action="task_created",
            request_tag=request_tag,
            arguments={"title": clean_title, "description": description, "due_date": due_date},
            operation=operation,
        )

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/update", dependencies=[Depends(require_access_token)])
    async def update_task(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        title: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        due_date: Annotated[date | None, Query()] = None,
    ) -> JSONResponse:
        clean_title = nonblank(title, field_name="title") if title is not None else None
        if clean_title is None and due_date is None:
            raise ApiProblem(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "invalid_request",
                "Provide title or due_date.",
            )
        update_payload: dict[str, Any] = {}
        if clean_title is not None:
            update_payload["title"] = clean_title
        if due_date is not None:
            update_payload["due_date"] = f"{due_date.isoformat()}T00:00:00Z"

        async def operation() -> dict[str, Any]:
            await services.vikunja.task_in_project(task_id)
            task = await services.vikunja.update_task(task_id, update_payload)
            return {"task": task_view(task)}

        return await run_mutation(
            services,
            action="task_updated",
            request_tag=request_tag,
            arguments={"task_id": task_id, **update_payload},
            operation=operation,
        )

    @app.get(
        f"{API_PREFIX}/tasks/{{task_id}}/complete", dependencies=[Depends(require_access_token)]
    )
    async def complete_task(
        services: ServicesDep, task_id: TaskId, request_tag: RequestTag
    ) -> JSONResponse:
        async def operation() -> dict[str, Any]:
            await services.vikunja.task_in_project(task_id)
            task = await services.vikunja.update_task(task_id, {"done": True})
            return {"task": task_view(task)}

        return await run_mutation(
            services,
            action="task_completed",
            request_tag=request_tag,
            arguments={"task_id": task_id},
            operation=operation,
        )

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/reopen", dependencies=[Depends(require_access_token)])
    async def reopen_task(
        services: ServicesDep, task_id: TaskId, request_tag: RequestTag
    ) -> JSONResponse:
        async def operation() -> dict[str, Any]:
            await services.vikunja.task_in_project(task_id)
            task = await services.vikunja.update_task(task_id, {"done": False})
            return {"task": task_view(task)}

        return await run_mutation(
            services,
            action="task_reopened",
            request_tag=request_tag,
            arguments={"task_id": task_id},
            operation=operation,
        )

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/delete", dependencies=[Depends(require_access_token)])
    async def delete_task(
        services: ServicesDep, task_id: TaskId, request_tag: RequestTag
    ) -> JSONResponse:
        async def operation() -> dict[str, Any]:
            task = await services.vikunja.task_in_project(task_id)
            await services.vikunja.delete_task(task_id)
            return {"task": task_view(task), "deleted": True}

        return await run_mutation(
            services,
            action="task_deleted",
            request_tag=request_tag,
            arguments={"task_id": task_id},
            operation=operation,
        )

    @app.get(
        f"{API_PREFIX}/tasks/{{task_id}}/comment/add", dependencies=[Depends(require_access_token)]
    )
    async def add_comment(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        text: Annotated[str, Query(min_length=1, max_length=20_000)],
    ) -> JSONResponse:
        clean_text = nonblank(text, field_name="text")

        async def operation() -> dict[str, Any]:
            await services.vikunja.task_in_project(task_id)
            comment = await services.vikunja.add_comment(task_id, clean_text)
            task = await services.vikunja.get_task(task_id)
            return {"task": task_view(task), "comment": comment_view(comment)}

        return await run_mutation(
            services,
            action="comment_added",
            request_tag=request_tag,
            arguments={"task_id": task_id, "text": clean_text},
            operation=operation,
        )

    @app.get(
        f"{API_PREFIX}/tasks/{{task_id}}/label/add", dependencies=[Depends(require_access_token)]
    )
    async def add_label(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        label: Annotated[str, Query(min_length=1, max_length=250)],
    ) -> JSONResponse:
        clean_label = nonblank(label, field_name="label")

        async def operation() -> dict[str, Any]:
            await services.vikunja.task_in_project(task_id)
            await services.vikunja.add_label(task_id, clean_label)
            task = await services.vikunja.get_task(task_id)
            return {"task": task_view(task)}

        return await run_mutation(
            services,
            action="label_added",
            request_tag=request_tag,
            arguments={"task_id": task_id, "label": clean_label},
            operation=operation,
        )

    @app.get(
        f"{API_PREFIX}/tasks/{{task_id}}/label/remove", dependencies=[Depends(require_access_token)]
    )
    async def remove_label(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        label: Annotated[str, Query(min_length=1, max_length=250)],
    ) -> JSONResponse:
        clean_label = nonblank(label, field_name="label")

        async def operation() -> dict[str, Any]:
            task = await services.vikunja.task_in_project(task_id)
            await services.vikunja.remove_label(task, clean_label)
            updated_task = await services.vikunja.get_task(task_id)
            return {"task": task_view(updated_task)}

        return await run_mutation(
            services,
            action="label_removed",
            request_tag=request_tag,
            arguments={"task_id": task_id, "label": clean_label},
            operation=operation,
        )

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}", dependencies=[Depends(require_access_token)])
    async def get_task(services: ServicesDep, task_id: TaskId) -> JSONResponse:
        task = await services.vikunja.task_in_project(task_id)
        return json_response({"ok": True, "action": "task_retrieved", "task": task_view(task)})

    return app


def main() -> None:
    uvicorn.run("vikuget.app:create_app", factory=True, host="0.0.0.0", port=8000, access_log=False)
