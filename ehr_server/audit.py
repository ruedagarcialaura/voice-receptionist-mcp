"""Append-only audit log of every tool call, including rejected ones.

Recording failures matters as much as successes: "the agent tried to book
Dr. Johnson on a Tuesday and the server refused" is exactly what you want to
find when reviewing a call or turning it into an eval case.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


def _appointment_id(args: dict[str, Any], result: dict[str, Any]) -> str | None:
    appt = result.get("appointment") or {}
    return appt.get("appointment_id") or args.get("appointment_id")


class AuditLog:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect

    def record(self, tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        error = result.get("error") or {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, tool, args_json, ok, error_code, appointment_id, result_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    tool,
                    json.dumps(args, default=str),
                    int(bool(result.get("ok"))),
                    error.get("code"),
                    _appointment_id(args, result),
                    json.dumps(result, default=str),
                ),
            )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def for_appointment(self, appointment_id: str) -> list[dict[str, Any]]:
        """History of one appointment, including the reschedule that replaced it."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE appointment_id = ? "
                "OR json_extract(result_json, '$.replaced_appointment_id') = ? ORDER BY id",
                (appointment_id, appointment_id),
            ).fetchall()
        return [_row(r) for r in rows]


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"],
        "tool": r["tool"],
        "args": json.loads(r["args_json"]),
        "ok": bool(r["ok"]),
        "error_code": r["error_code"],
        "appointment_id": r["appointment_id"],
    }
