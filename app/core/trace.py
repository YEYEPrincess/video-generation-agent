"""事件（trace）：所有可观测性都从这里出。

type 枚举固定 10 个，禁止随手新增——面试时这张表就是"我懂可观测"的证据。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from app.db import iso

EVENT_TYPES = {
    "state_transition",
    "stage_start",
    "stage_done",
    "stage_skip",
    "tool_call",
    "tool_result",
    "llm_call",
    "validation_fail",
    "retry",
    "lease",
}


def emit(
    conn: sqlite3.Connection,
    task_id: str,
    type_: str,
    *,
    stage: str | None = None,
    level: str = "info",
    duration_ms: int | None = None,
    tokens: int | None = None,
    cost_cents: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    assert type_ in EVENT_TYPES, f"unknown event type: {type_}"
    conn.execute(
        "INSERT INTO events(task_id, ts, level, stage, type, duration_ms, tokens, cost_cents, payload_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            iso(),
            level,
            stage,
            type_,
            duration_ms,
            tokens,
            cost_cents,
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        ),
    )


@contextmanager
def timer() -> Iterator[dict[str, int]]:
    """用法：with timer() as t: ...  之后 t['ms'] 就是耗时。"""
    box = {"ms": 0}
    start = time.perf_counter()
    try:
        yield box
    finally:
        box["ms"] = int((time.perf_counter() - start) * 1000)
