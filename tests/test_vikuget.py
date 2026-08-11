from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient

from vikuget import Settings, create_app


def make_client(tmp_path, handler: Callable[[httpx.Request], httpx.Response]) -> TestClient:
    settings = Settings(
        vikunja_url="http://vikunja:3456",
        vikunja_token="vikunja-token",
        vikunja_project_id=17,
        access_token="a" * 32,
        request_store_path=tmp_path / "idempotency.sqlite3",
    )
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def api_headers() -> dict[str, str]:
    return {"X-Forwarded-Proto": "https"}


def api_path(path: str, token: str = "a" * 32, request_tag: str = "read:1") -> str:
    return f"/v1/{token}/{request_tag}{path}"


def test_create_is_replayed_without_second_vikunja_call(tmp_path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "PUT"
        assert request.url.path == "/api/v1/projects/17/tasks"
        return httpx.Response(
            201,
            json={
                "id": 42,
                "title": "Buy SSD",
                "description": "",
                "done": False,
                "due_date": "2026-08-14T00:00:00Z",
                "labels": [],
            },
        )

    with make_client(tmp_path, handler) as client:
        first = client.get(
            api_path("/tasks/create", request_tag="create:42"),
            params={"title": "Buy SSD", "due_date": "2026-08-14"},
            headers=api_headers(),
        )
        second = client.get(
            api_path("/tasks/create", request_tag="create:42"),
            params={"title": "Buy SSD", "due_date": "2026-08-14"},
            headers=api_headers(),
        )

    assert first.status_code == 200
    assert first.json() == {
        "ok": True,
        "action": "task_created",
        "task": {
            "id": 42,
            "title": "Buy SSD",
            "description": "",
            "done": False,
            "due_date": "2026-08-14",
            "labels": [],
        },
    }
    assert second.json() == first.json()
    assert second.headers["idempotent-replay"] == "true"
    assert second.headers["cache-control"] == "no-store, no-cache, max-age=0, private"
    assert len(calls) == 1
    assert calls[0].content == b'{"title":"Buy SSD","due_date":"2026-08-14T00:00:00Z"}'


def test_task_outside_configured_project_is_hidden_and_not_modified(tmp_path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": 99, "project_id": 18, "title": "Other task"})

    with make_client(tmp_path, handler) as client:
        response = client.get(
            api_path("/tasks/99/complete", request_tag="complete:99"),
            headers=api_headers(),
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "task_not_found"
    assert [call.method for call in calls] == ["GET"]


def test_list_and_search_use_only_the_configured_project_view(tmp_path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/v1/projects/17/views":
            return httpx.Response(200, json=[{"id": 7, "view_kind": "list"}])
        assert request.url.path == "/api/v1/projects/17/views/7/tasks"
        assert request.url.params["s"] == "ssd"
        assert request.url.params["page"] == "2"
        assert request.url.params["per_page"] == "20"
        return httpx.Response(
            200,
            headers={"X-Total-Count": "1"},
            json=[{"id": 42, "title": "Buy SSD", "done": False, "labels": []}],
        )

    with make_client(tmp_path, handler) as client:
        response = client.get(
            api_path("/tasks/search"),
            params={"q": "ssd", "page": 2, "per_page": 20},
            headers=api_headers(),
        )

    assert response.status_code == 200
    assert response.json()["pagination"] == {"page": 2, "per_page": 20, "count": 1, "total": 1}
    assert len(calls) == 2


def test_http_query_tokens_and_invalid_path_tokens_are_rejected(tmp_path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Vikunja must not be called")

    with make_client(tmp_path, handler) as client:
        insecure = client.get(api_path("/tasks"))
        query_token = client.get(
            api_path("/tasks"), params={"access_token": "a" * 32}, headers=api_headers()
        )
        invalid_path_token = client.get(api_path("/tasks", "b" * 32), headers=api_headers())

    assert insecure.status_code == 400
    assert insecure.json()["error"]["code"] == "https_required"
    assert query_token.status_code == 400
    assert query_token.json()["error"]["code"] == "query_token_forbidden"
    assert invalid_path_token.status_code == 401
