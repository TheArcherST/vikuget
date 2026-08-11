from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from fastapi import status

from .errors import ApiProblem


def task_view(task: Mapping[str, Any]) -> dict[str, Any]:
    labels = task.get("labels", [])
    label_titles = (
        [
            item["title"]
            for item in labels
            if isinstance(item, dict) and isinstance(item.get("title"), str)
        ]
        if isinstance(labels, list)
        else []
    )
    raw_due_date = task.get("due_date")
    due_date = None
    if isinstance(raw_due_date, str) and raw_due_date and not raw_due_date.startswith("0001-01-01"):
        due_date = raw_due_date[:10]
    return {
        "id": task.get("id"),
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "done": bool(task.get("done", False)),
        "due_date": due_date,
        "labels": label_titles,
    }


def comment_view(comment: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": comment.get("id"), "text": comment.get("comment", "")}


def fingerprint(action: str, arguments: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        {"action": action, "arguments": arguments},
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def nonblank(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ApiProblem(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_request",
            f"{field_name} must not be blank.",
        )
    return normalized
