from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
from fastapi import status

from .config import Settings
from .errors import ApiProblem


class VikunjaClient:
    """The only component allowed to speak Vikunja's API."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self.client: httpx.AsyncClient | None = None
        self._project_view_id = settings.vikunja_project_view_id

    async def open(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.settings.vikunja_url,
            headers={
                "Authorization": f"Bearer {self.settings.vikunja_token.get_secret_value()}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(self.settings.vikunja_timeout_seconds),
            follow_redirects=False,
            transport=self.transport,
        )

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def get_task(self, task_id: int) -> dict[str, Any]:
        return self._object(await self.request("GET", f"/api/v1/tasks/{task_id}"))

    async def task_in_project(self, task_id: int) -> dict[str, Any]:
        task = await self.get_task(task_id)
        if task.get("project_id") != self.settings.vikunja_project_id:
            # Do not disclose the existence of tasks outside the fixed project.
            raise ApiProblem(status.HTTP_404_NOT_FOUND, "task_not_found", "Task not found.")
        return task

    async def list_tasks(
        self, *, page: int, per_page: int, query: str | None = None
    ) -> tuple[list[dict[str, Any]], int | None]:
        view_id = await self.project_view_id()
        params: dict[str, str | int] = {"page": page, "per_page": per_page}
        if query is not None:
            params["s"] = query
        payload, headers = await self.request_with_headers(
            "GET",
            f"/api/v1/projects/{self.settings.vikunja_project_id}/views/{view_id}/tasks",
            params=params,
        )
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_response_invalid",
                "Invalid response from Vikunja.",
            )
        total = headers.get("x-total-count")
        return payload, int(total) if total and total.isdigit() else None

    async def create_task(
        self, *, title: str, description: str | None, due_date: date | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title}
        if description is not None:
            payload["description"] = description
        if due_date is not None:
            payload["due_date"] = f"{due_date.isoformat()}T00:00:00Z"
        return self._object(
            await self.request(
                "PUT",
                f"/api/v1/projects/{self.settings.vikunja_project_id}/tasks",
                json=payload,
            )
        )

    async def update_task(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._object(await self.request("POST", f"/api/v1/tasks/{task_id}", json=payload))

    async def delete_task(self, task_id: int) -> None:
        await self.request("DELETE", f"/api/v1/tasks/{task_id}")

    async def add_comment(self, task_id: int, text: str) -> dict[str, Any]:
        return self._object(
            await self.request("PUT", f"/api/v1/tasks/{task_id}/comments", json={"comment": text})
        )

    async def add_label(self, task_id: int, label: str) -> None:
        label_id = await self.label_id(label)
        await self.request("PUT", f"/api/v1/tasks/{task_id}/labels", json={"label_id": label_id})

    async def remove_label(self, task: dict[str, Any], label: str) -> None:
        labels = task.get("labels", [])
        if not isinstance(labels, list):
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_response_invalid",
                "Invalid response from Vikunja.",
            )
        target = next(
            (
                item
                for item in labels
                if isinstance(item, dict)
                and isinstance(item.get("title"), str)
                and item["title"].casefold() == label.casefold()
            ),
            None,
        )
        if target is None or not isinstance(target.get("id"), int):
            raise ApiProblem(
                status.HTTP_404_NOT_FOUND, "label_not_found", "Label is not on this task."
            )
        await self.request("DELETE", f"/api/v1/tasks/{task['id']}/labels/{target['id']}")

    async def label_id(self, title: str) -> int:
        payload = await self.request("GET", "/api/v1/labels", params={"s": title, "per_page": 50})
        if not isinstance(payload, list):
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_response_invalid",
                "Invalid response from Vikunja.",
            )
        for item in payload:
            if (
                isinstance(item, dict)
                and isinstance(item.get("id"), int)
                and isinstance(item.get("title"), str)
                and item["title"].casefold() == title.casefold()
            ):
                return item["id"]
        created = self._object(await self.request("PUT", "/api/v1/labels", json={"title": title}))
        label_id = created.get("id")
        if not isinstance(label_id, int):
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_response_invalid",
                "Invalid response from Vikunja.",
            )
        return label_id

    async def project_view_id(self) -> int:
        if self._project_view_id is not None:
            return self._project_view_id
        views = await self.request(
            "GET", f"/api/v1/projects/{self.settings.vikunja_project_id}/views"
        )
        if not isinstance(views, list):
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_response_invalid",
                "Invalid response from Vikunja.",
            )
        for view in views:
            if (
                isinstance(view, dict)
                and view.get("view_kind") == "list"
                and isinstance(view.get("id"), int)
            ):
                self._project_view_id = view["id"]
                return self._project_view_id
        raise ApiProblem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "project_view_unavailable",
            "The configured project has no List view. Set VIKUNJA_PROJECT_VIEW_ID explicitly.",
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        payload, _ = await self.request_with_headers(method, path, **kwargs)
        return payload

    async def request_with_headers(
        self, method: str, path: str, **kwargs: Any
    ) -> tuple[Any, httpx.Headers]:
        if self.client is None:
            raise RuntimeError("Vikunja client is not open")
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise ApiProblem(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "vikunja_unavailable",
                "Vikunja is currently unavailable.",
            ) from error
        if response.is_error:
            if 400 <= response.status_code < 500:
                raise ApiProblem(
                    response.status_code,
                    "vikunja_request_rejected",
                    "Vikunja rejected the request.",
                )
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_unavailable",
                "Vikunja is currently unavailable.",
            )
        if not response.content:
            return {}, response.headers
        try:
            return response.json(), response.headers
        except json.JSONDecodeError as error:
            raise ApiProblem(
                status.HTTP_502_BAD_GATEWAY,
                "vikunja_response_invalid",
                "Invalid response from Vikunja.",
            ) from error

    @staticmethod
    def _object(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        raise ApiProblem(
            status.HTTP_502_BAD_GATEWAY,
            "vikunja_response_invalid",
            "Invalid response from Vikunja.",
        )
