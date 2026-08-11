from __future__ import annotations

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
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .access import client_ip_from_x_forwarded_for
from .client import VikunjaClient
from .config import Settings
from .errors import ApiProblem, error_body, error_response, result_response
from .replays import CachedResponse, RequestReplayStore
from .views import comment_view, fingerprint, nonblank, task_view

logger = logging.getLogger("uvicorn.error")

API_PREFIX = "/{request_tag}"
LEGACY_API_PREFIX = "/v1"


@dataclass
class Services:
    settings: Settings
    vikunja: VikunjaClient
    replays: RequestReplayStore


async def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]
RequestTag = Annotated[str, Path(min_length=1, max_length=1024)]
TaskId = Annotated[int, Path(ge=1)]


async def require_request_tag(request_tag: RequestTag) -> None:
    if not request_tag.strip():
        raise ApiProblem(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_request",
            "request_tag must not be blank.",
        )


ApiDependencies = [Depends(require_request_tag)]


async def run_mutation(
    services: Services,
    *,
    action: str,
    request_tag: str,
    arguments: Mapping[str, Any],
    operation: Callable[[], Awaitable[dict[str, Any]]],
) -> HTMLResponse:
    cached = services.replays.claim(
        request_tag=request_tag,
        fingerprint=fingerprint(action, arguments),
    )
    if cached is not None:
        return result_response(
            body=cached.body,
            extra_headers={"Idempotent-Replay": "true"},
        )

    try:
        response = CachedResponse(
            body={"ok": True, "action": action, **await operation()},
        )
    except ApiProblem as problem:
        response = CachedResponse(
            body=error_body(action=action, code=problem.code, message=problem.message),
        )
    except Exception:
        logger.exception("Unhandled mutation failure for action %s", action)
        response = CachedResponse(
            body=error_body(
                action=action,
                code="internal_error",
                message="The action could not be completed safely.",
            ),
        )

    services.replays.complete(request_tag=request_tag, response=response)
    return result_response(body=response.body)


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
    async def authorize_and_log_request(
        request: Request, call_next: Callable[..., Awaitable[Any]]
    ) -> Any:
        original_path = request.scope["path"]
        is_healthcheck = original_path == "/health"
        if original_path.startswith(f"{LEGACY_API_PREFIX}/"):
            request.scope["path"] = original_path.removeprefix(LEGACY_API_PREFIX)

        if is_healthcheck:
            return await call_next(request)

        client_ip = "unknown"
        try:
            client_address = client_ip_from_x_forwarded_for(request.headers.get("x-forwarded-for"))
            client_ip = str(client_address)
        except ValueError:
            response = error_response(
                action="request_rejected",
                code="client_ip_unavailable",
                message="The client IP address is unavailable.",
            )
        else:
            if request.method != "GET":
                response = error_response(
                    action="request_rejected",
                    code="method_not_allowed",
                    message="Only GET requests are accepted.",
                )
            elif request.headers.get("x-forwarded-proto") != "https":
                response = error_response(
                    action="request_rejected",
                    code="https_required",
                    message="HTTPS is required.",
                )
            elif not services.settings.allowed_ips.allows(client_address):
                response = error_response(
                    action="request_rejected",
                    code="ip_not_allowed",
                    message="This IP address is not allowed.",
                )
            else:
                response = await call_next(request)
        logger.info(
            "vikuget response client_ip=%s method=%s status=%s",
            client_ip,
            request.method,
            response.status_code,
        )
        return response

    @app.exception_handler(ApiProblem)
    async def handle_api_problem(_: Request, problem: ApiProblem) -> HTMLResponse:
        return error_response(
            action="request_rejected",
            code=problem.code,
            message=problem.message,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, __: RequestValidationError) -> HTMLResponse:
        return error_response(
            action="request_rejected",
            code="invalid_request",
            message="Request parameters are invalid.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, problem: StarletteHTTPException) -> HTMLResponse:
        code = (
            "not_found" if problem.status_code == status.HTTP_404_NOT_FOUND else "request_rejected"
        )
        message = "Endpoint not found." if code == "not_found" else "Request rejected."
        return error_response(
            action="request_rejected",
            code=code,
            message=message,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, problem: Exception) -> HTMLResponse:
        logger.exception("Unhandled request failure", exc_info=problem)
        return error_response(
            action="request_rejected",
            code="internal_error",
            message="The request could not be completed safely.",
        )

    @app.get("/health", include_in_schema=False)
    async def health() -> HTMLResponse:
        return result_response(
            body={"ok": True, "action": "health_checked", "status": "ok"},
        )

    @app.get(f"{API_PREFIX}/tasks", dependencies=ApiDependencies)
    async def list_tasks(
        services: ServicesDep,
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> HTMLResponse:
        tasks, total = await services.vikunja.list_tasks(page=page, per_page=per_page)
        result: dict[str, Any] = {
            "ok": True,
            "action": "tasks_listed",
            "tasks": [task_view(task) for task in tasks],
            "pagination": {"page": page, "per_page": per_page, "count": len(tasks)},
        }
        if total is not None:
            result["pagination"]["total"] = total
        return result_response(body=result)

    @app.get(f"{API_PREFIX}/tasks/search", dependencies=ApiDependencies)
    async def search_tasks(
        services: ServicesDep,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> HTMLResponse:
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
        return result_response(body=result)

    @app.get(f"{API_PREFIX}/tasks/create", dependencies=ApiDependencies)
    async def create_task(
        services: ServicesDep,
        request_tag: RequestTag,
        title: Annotated[str, Query(min_length=1, max_length=1024)],
        description: Annotated[str | None, Query(max_length=20_000)] = None,
        due_date: Annotated[date | None, Query()] = None,
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/update", dependencies=ApiDependencies)
    async def update_task(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        title: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        due_date: Annotated[date | None, Query()] = None,
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/complete", dependencies=ApiDependencies)
    async def complete_task(
        services: ServicesDep, task_id: TaskId, request_tag: RequestTag
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/reopen", dependencies=ApiDependencies)
    async def reopen_task(
        services: ServicesDep, task_id: TaskId, request_tag: RequestTag
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/delete", dependencies=ApiDependencies)
    async def delete_task(
        services: ServicesDep, task_id: TaskId, request_tag: RequestTag
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/comment/add", dependencies=ApiDependencies)
    async def add_comment(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        text: Annotated[str, Query(min_length=1, max_length=20_000)],
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/label/add", dependencies=ApiDependencies)
    async def add_label(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        label: Annotated[str, Query(min_length=1, max_length=250)],
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/label/remove", dependencies=ApiDependencies)
    async def remove_label(
        services: ServicesDep,
        task_id: TaskId,
        request_tag: RequestTag,
        label: Annotated[str, Query(min_length=1, max_length=250)],
    ) -> HTMLResponse:
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

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}", dependencies=ApiDependencies)
    async def get_task(services: ServicesDep, task_id: TaskId) -> HTMLResponse:
        task = await services.vikunja.task_in_project(task_id)
        return result_response(
            body={"ok": True, "action": "task_retrieved", "task": task_view(task)},
        )

    return app


def main() -> None:
    uvicorn.run("vikuget.app:create_app", factory=True, host="0.0.0.0", port=8000, access_log=False)
