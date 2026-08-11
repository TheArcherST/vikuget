from __future__ import annotations

from html import escape
from typing import Any

from fastapi import status
from fastapi.responses import HTMLResponse

NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, max-age=0, private",
    "Pragma": "no-cache",
    "Expires": "0",
}

ACTION_TITLES = {
    "health_checked": "Сервис доступен",
    "tasks_listed": "Список задач",
    "tasks_searched": "Результаты поиска",
    "task_retrieved": "Задача",
    "task_created": "Задача создана",
    "task_updated": "Задача изменена",
    "task_completed": "Задача выполнена",
    "task_reopened": "Задача открыта",
    "task_deleted": "Задача удалена",
    "comment_added": "Комментарий добавлен",
    "label_added": "Метка добавлена",
    "label_removed": "Метка снята",
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
    extra_headers: dict[str, str] | None = None,
) -> HTMLResponse:
    headers = {**NO_STORE_HEADERS, **(extra_headers or {})}
    return HTMLResponse(
        status_code=status.HTTP_200_OK,
        content=html_document(body),
        headers=headers,
    )


def error_response(*, action: str, code: str, message: str) -> HTMLResponse:
    return result_response(
        body=error_body(action=action, code=code, message=message),
    )


def html_document(body: dict[str, Any]) -> str:
    """Render the small, human-readable HTML representation of a gateway result."""

    title = _page_title(body)
    content = _error_content(body) if body.get("ok") is False else _success_content(body)
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head><body><main>{content}</main></body></html>"
    )


def _page_title(body: dict[str, Any]) -> str:
    if body.get("ok") is False:
        return "Запрос не выполнен"
    action = body.get("action")
    if isinstance(action, str):
        return ACTION_TITLES.get(action, "Результат запроса")
    return "Результат"


def _error_content(body: dict[str, Any]) -> str:
    error = body.get("error")
    if not isinstance(error, dict):
        return "<h1>Запрос не выполнен</h1><p>Причина ошибки не указана.</p>"
    code = _text(error.get("code"), "unknown_error")
    message = _text(error.get("message"), "Причина ошибки не указана.")
    return (
        "<h1>Запрос не выполнен</h1>"
        f"<p>{_multiline(message)}</p>"
        "<dl><dt>Код ошибки</dt>"
        f"<dd><code>{escape(code)}</code></dd></dl>"
    )


def _success_content(body: dict[str, Any]) -> str:
    sections: list[str] = [f"<h1>{escape(_page_title(body))}</h1>"]

    status_text = body.get("status")
    if isinstance(status_text, str):
        sections.append(f"<p>{_multiline(status_text)}</p>")

    task = body.get("task")
    if isinstance(task, dict):
        sections.append(_task_section(task, heading="Задача"))

    comment = body.get("comment")
    if isinstance(comment, dict):
        sections.append(_comment_section(comment))

    tasks = body.get("tasks")
    if isinstance(tasks, list):
        sections.append(_tasks_section(tasks))

    pagination = body.get("pagination")
    if isinstance(pagination, dict):
        sections.append(_pagination_section(pagination))

    if body.get("deleted") is True:
        sections.append("<p>Задача удалена.</p>")

    if len(sections) == 1:
        sections.append("<p>Запрос выполнен.</p>")
    return "".join(sections)


def _task_section(task: dict[str, Any], *, heading: str) -> str:
    return f"<section><h2>{escape(heading)}</h2>{_task_details(task)}</section>"


def _task_details(task: dict[str, Any]) -> str:
    task_id = _text(task.get("id"), "—")
    title = _text(task.get("title"), "Без названия")
    description = _text(task.get("description"), "—")
    due_date = _text(task.get("due_date"), "Не назначен")
    done = "Да" if task.get("done") is True else "Нет"
    details = (
        "<dl>"
        f"<dt>ID</dt><dd>{escape(task_id)}</dd>"
        f"<dt>Название</dt><dd>{_multiline(title)}</dd>"
        f"<dt>Описание</dt><dd>{_multiline(description)}</dd>"
        f"<dt>Выполнена</dt><dd>{done}</dd>"
        f"<dt>Срок</dt><dd>{escape(due_date)}</dd>"
        "</dl>"
    )
    labels = task.get("labels")
    if not isinstance(labels, list) or not labels:
        return f"{details}<h3>Метки</h3><p>Нет</p>"
    items = "".join(f"<li>{_multiline(_text(label, ''))}</li>" for label in labels)
    return f"{details}<h3>Метки</h3><ul>{items}</ul>"


def _comment_section(comment: dict[str, Any]) -> str:
    comment_id = _text(comment.get("id"), "—")
    text = _text(comment.get("text"), "—")
    return (
        "<section><h2>Комментарий</h2><dl>"
        f"<dt>ID</dt><dd>{escape(comment_id)}</dd>"
        f"<dt>Текст</dt><dd>{_multiline(text)}</dd>"
        "</dl></section>"
    )


def _tasks_section(tasks: list[Any]) -> str:
    items: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = _text(task.get("id"), "—")
        title = _text(task.get("title"), "Без названия")
        items.append(
            "<li>"
            f"<h3>{_multiline(title)}</h3>"
            f"<p>ID: {escape(task_id)}</p>"
            f"{_task_details(task)}"
            "</li>"
        )
    if not items:
        return "<section><h2>Задачи</h2><p>Нет задач.</p></section>"
    return f"<section><h2>Задачи</h2><ol>{''.join(items)}</ol></section>"


def _pagination_section(pagination: dict[str, Any]) -> str:
    fields = (
        ("page", "Страница"),
        ("per_page", "На странице"),
        ("count", "Показано"),
        ("total", "Всего"),
    )
    items = "".join(
        f"<dt>{label}</dt><dd>{escape(_text(pagination[key], '—'))}</dd>"
        for key, label in fields
        if key in pagination
    )
    return f"<section><h2>Навигация</h2><dl>{items}</dl></section>" if items else ""


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) else str(value) if value is not None else fallback


def _multiline(value: str) -> str:
    return escape(value).replace("\n", "<br>")
