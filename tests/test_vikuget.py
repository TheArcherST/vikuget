from __future__ import annotations

import logging
from collections.abc import Callable
from html.parser import HTMLParser

import httpx
from fastapi.testclient import TestClient

from vikuget import Settings, create_app


def make_client(
    tmp_path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    allowed_ips: str = "198.51.100.0/24",
) -> TestClient:
    settings = Settings(
        vikunja_url="http://vikunja:3456",
        vikunja_token="vikunja-token",
        vikunja_project_id=17,
        allowed_ips=allowed_ips,
        request_store_path=tmp_path / "idempotency.sqlite3",
    )
    return TestClient(create_app(settings, httpx.MockTransport(handler)))


def api_headers(client_ip: str = "198.51.100.23") -> dict[str, str]:
    return {"X-Forwarded-For": client_ip, "X-Forwarded-Proto": "https"}


def api_path(path: str, request_tag: str = "read:1") -> str:
    return f"/v1/{request_tag}{path}"


class HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def page_text(response: httpx.Response) -> str:
    assert response.headers["content-type"].startswith("text/html")
    assert "<script" not in response.text
    parser = HtmlTextExtractor()
    parser.feed(response.text)
    return " ".join(" ".join(parser.parts).split())


def test_create_is_replayed_without_second_vikunja_call(tmp_path, caplog) -> None:
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

    caplog.set_level(logging.INFO, logger="uvicorn.error")
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
    first_text = page_text(first)
    assert "Задача создана" in first_text
    assert "ID 42" in first_text
    assert "Название Buy SSD" in first_text
    assert "Срок 2026-08-14" in first_text
    assert page_text(second) == first_text
    assert second.headers["idempotent-replay"] == "true"
    assert second.headers["cache-control"] == "no-store, no-cache, max-age=0, private"
    assert len(calls) == 1
    assert calls[0].content == b'{"title":"Buy SSD","due_date":"2026-08-14T00:00:00Z"}'
    assert "vikuget response client_ip=198.51.100.23 method=GET status=200" in caplog.text


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

    assert response.status_code == 200
    text = page_text(response)
    assert "Запрос не выполнен" in text
    assert "Код ошибки task_not_found" in text
    assert "Task not found." in text
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
    text = page_text(response)
    assert "Результаты поиска" in text
    assert "Название Buy SSD" in text
    assert "Навигация Страница 2 На странице 20 Показано 1 Всего 1" in text
    assert len(calls) == 2


def test_ip_access_and_rejected_requests_are_reported(tmp_path, caplog) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("Vikunja must not be called")

    caplog.set_level(logging.INFO, logger="uvicorn.error")
    with make_client(tmp_path, handler) as client:
        insecure = client.get(api_path("/tasks"), headers={"X-Forwarded-For": "198.51.100.23"})
        denied_ip = client.get(api_path("/tasks"), headers=api_headers("203.0.113.9"))
        missing_client_ip = client.get(api_path("/tasks"), headers={"X-Forwarded-Proto": "https"})
        missing_endpoint = client.get(api_path("/missing"), headers=api_headers())
        wrong_method = client.post(api_path("/tasks"), headers=api_headers())

    assert insecure.status_code == 200
    assert "Код ошибки https_required" in page_text(insecure)
    assert denied_ip.status_code == 200
    assert "Код ошибки ip_not_allowed" in page_text(denied_ip)
    assert missing_client_ip.status_code == 200
    assert "Код ошибки client_ip_unavailable" in page_text(missing_client_ip)
    assert missing_endpoint.status_code == 200
    assert "Код ошибки not_found" in page_text(missing_endpoint)
    assert wrong_method.status_code == 200
    assert "Код ошибки method_not_allowed" in page_text(wrong_method)
    assert "vikuget response client_ip=198.51.100.23 method=GET status=200" in caplog.text
    assert "vikuget response client_ip=203.0.113.9 method=GET status=200" in caplog.text
