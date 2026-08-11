from __future__ import annotations

import hmac
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import status

from .errors import ApiProblem


@dataclass(frozen=True)
class CachedResponse:
    body: dict[str, Any]


class RequestReplayStore:
    """Durable idempotency state for GET actions with externally controlled tags.

    A pending tag is intentionally never retried automatically. After an uncertain
    upstream failure, repeating a request could create a second task.
    """

    def __init__(self, path: Path, retention_days: int):
        self.path = path
        self.retention_seconds = retention_days * 24 * 60 * 60
        self.connection: sqlite3.Connection | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS request_replays (
                request_tag TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                response_status INTEGER,
                response_json TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.connection = connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def claim(self, *, request_tag: str, fingerprint: str) -> CachedResponse | None:
        connection = self._connection()
        now = int(time.time())
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM request_replays WHERE created_at < ?",
                (now - self.retention_seconds,),
            )
            row = connection.execute(
                """
                SELECT fingerprint, response_status, response_json
                FROM request_replays
                WHERE request_tag = ?
                """,
                (request_tag,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO request_replays (request_tag, fingerprint, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (request_tag, fingerprint, now),
                )
                connection.execute("COMMIT")
                return None
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

        stored_fingerprint, response_status, response_json = row
        if not hmac.compare_digest(stored_fingerprint, fingerprint):
            raise ApiProblem(
                status.HTTP_409_CONFLICT,
                "request_tag_reused",
                "request_tag was already used for a different action or parameters",
            )
        if response_status is None or response_json is None:
            raise ApiProblem(
                status.HTTP_409_CONFLICT,
                "request_in_progress",
                "The original request has an unknown outcome and will not be repeated.",
            )
        return CachedResponse(body=json.loads(response_json))

    def complete(self, *, request_tag: str, response: CachedResponse) -> None:
        connection = self._connection()
        connection.execute(
            """
            UPDATE request_replays
            SET response_status = ?, response_json = ?
            WHERE request_tag = ?
            """,
            (
                status.HTTP_200_OK,
                json.dumps(response.body, ensure_ascii=False, separators=(",", ":")),
                request_tag,
            ),
        )

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Request replay store is not open")
        return self.connection
