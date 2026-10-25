"""阶段执行器：状态机 + 幂等 + 重试 + 落产物的唯一入口。

一次任务的执行循环：
    get task → 取 current_stage → 幂等命中? → handler → 契约校验 → 校验器
    → 落产物 → 推进 current_stage → 直到 QC 之后 → DONE
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.contracts import STAGE_CONTRACTS, Check
from app.core.context import StageContext, StageResult
from app.core.idem import idem_key, short_hash
from app.core.queue import Queue
from app.core.state_machine import (
    STAGE_BUDGET_CENTS,
    Stage,
    StageStatus,
    TaskStatus,
    next_stage,
    prev_stage,
)
from app.core.trace import emit, timer
from app.db import iso
from app.errors import AppError, ErrCode
from app.settings import Settings
from app.stages.registry import get_handler
from app.stages.validators import validate


# --------------------------------------------------------------------- helpers
def get_task(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise AppError(ErrCode.E_INTERNAL, f"task not found: {task_id}")
    return dict(row)


def get_stage_row(conn: sqlite3.Connection, task_id: str, stage: Stage) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM stages WHERE task_id = ? AND stage = ?", (task_id, stage.value)
    ).fetchone()
    return dict(row) if row else None


def load_upstream(conn: sqlite3.Connection, settings: Settings, task_id: str, stage: Stage) -> dict:
    """读取上一阶段的产物 JSON；RESEARCH 没有上游，返回 {}。"""
    prev = prev_stage(stage)
    if prev is None:
        return {}
    row = conn.execute(
        "SELECT path FROM artifacts WHERE task_id = ? AND stage = ? ORDER BY version DESC LIMIT 1",
        (task_id, prev.value),
    ).fetchone()
    if row is None:
        return {}
    path = Path(settings.artifacts_dir) / str(row["path"])
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_artifact(
    conn: sqlite3.Connection, settings: Settings, task_id: str, stage: Stage, data: dict
) -> tuple[str, str]:
    """落盘 + 登记 artifacts 表。返回 (artifact_id, 相对路径)。"""
    rel_dir = Path(task_id) / stage.value
    abs_dir = Path(settings.artifacts_dir) / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    version = int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM artifacts WHERE task_id = ? AND stage = ?",
            (task_id, stage.value),
        ).fetchone()["c"]
    ) + 1
    rel_path = (rel_dir / f"v{version}" / f"{stage.value.lower()}.json").as_posix()
    abs_path = Path(settings.artifacts_dir) / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    text = json.dumps(data, ensure_ascii=False, indent=2)
    abs_path.write_text(text, encoding="utf-8")

    artifact_id = "a_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO artifacts(id, task_id, stage, version, path, hash, kind, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (artifact_id, task_id, stage.value, version, rel_path, short_hash(text), "json", iso()),
    )
    return artifact_id, rel_path


def serialize_and_validate(stage: Stage, result: StageResult) -> dict:
    """契约（pydantic）→ 规则（validators），任一不过都抛 E_VALIDATION。"""
    data = result.data
    if result.validate_contract:
        model = STAGE_CONTRACTS[stage]
        try:
            data = model.model_validate(data).model_dump()
        except ValidationError as exc:
            raise AppError(
                ErrCode.E_VALIDATION,
                f"{stage.value} 契约校验失败",
                detail={"errors": exc.errors(include_url=False)[:3]},
            ) from exc

    checks: list[Check] = validate(stage, data)
    bad = [c for c in checks if c.severity == "block" and not c.passed]
    if bad:
        raise AppError(
            ErrCode.E_VALIDATION,
            f"{stage.value} 规则校验失败: " + "; ".join(f"{c.name}({c.detail})" for c in bad),
            detail={"failed": [c.model_dump() for c in bad]},
        )
    return data


def _advance(conn: sqlite3.Connection, task_id: str, stage: Stage, q: Queue) -> None:
    nxt = next_stage(stage)
    if nxt is None:
        conn.execute(
            "UPDATE tasks SET current_stage = NULL, updated_at = ? WHERE id = ?", (iso(), task_id)
        )
        q.finish(task_id, TaskStatus.DONE)
        return
    conn.execute(
        "UPDATE tasks SET current_stage = ?, updated_at = ? WHERE id = ?",
        (nxt.value, iso(), task_id),
    )
    emit(conn, task_id, "state_transition", payload={"advance_to": nxt.value})


def _fail_stage(conn: sqlite3.Connection, task_id: str, stage: Stage, err: AppError) -> None:
    conn.execute(
        "UPDATE stages SET status = ?, error = ?, finished_at = ? WHERE task_id = ? AND stage = ?",
        (StageStatus.FAILED.value, json.dumps(err.to_dict(), ensure_ascii=False), iso(), task_id, stage.value),
    )
    emit(conn, task_id, "validation_fail", stage=stage.value, level="error", payload=err.to_dict())


# --------------------------------------------------------------------- 单阶段
def run_one_stage(
    conn: sqlite3.Connection, task: dict[str, Any], stage: Stage, settings: Settings, q: Queue
) -> TaskStatus | None:
    """执行一个阶段。返回 None 表示成功推进；返回终态表示任务结束。"""
    task_id = str(task["id"])
    upstream = load_upstream(conn, settings, task_id, stage)
    key = idem_key(task_id, stage, upstream)
    row = get_stage_row(conn, task_id, stage)

    # ---- 幂等命中：直接复用，不重复调用模型、不重复扣费
    if row and row["status"] == StageStatus.DONE.value and row["idem_key"] == key:
        conn.execute(
            "UPDATE stages SET status = ? WHERE task_id = ? AND stage = ?",
            (StageStatus.SKIPPED.value, task_id, stage.value),
        )
        emit(conn, task_id, "stage_skip", stage=stage.value, payload={"idem_key": key})
        _advance(conn, task_id, stage, q)
        return None

    # ---- 预算闸
    spent = int(task["budget_spent_cents"])
    if spent >= settings.budget_cents:
        err = AppError(ErrCode.E_BUDGET, f"预算耗尽 {spent}c >= {settings.budget_cents}c", retryable=False)
        _fail_stage(conn, task_id, stage, err)
        return TaskStatus.PARTIAL

    handler = get_handler(stage)
    max_attempt = max(1, settings.max_attempt)

    for attempt in range(1, max_attempt + 1):
        conn.execute(
            "UPDATE stages SET status = ?, attempt = ?, started_at = ?, error = NULL"
            " WHERE task_id = ? AND stage = ?",
            (StageStatus.RUNNING.value, attempt, iso(), task_id, stage.value),
        )
        emit(conn, task_id, "stage_start", stage=stage.value, payload={"attempt": attempt, "idem_key": key})

        ctx = StageContext(
            task_id=task_id,
            topic=str(task["topic"]),
            partition=task.get("partition"),
            stage=stage,
            upstream=upstream,
            attempt=attempt,
            settings=settings,
            conn=conn,
        )

        with timer() as t:
            try:
                result = handler(ctx)
                data = serialize_and_validate(stage, result)
                artifact_id, rel_path = write_artifact(conn, settings, task_id, stage, data)
            except AppError as err:
                emit(
                    conn, task_id, "retry" if err.retryable and attempt < max_attempt else "validation_fail",
                    stage=stage.value, level="warn" if err.retryable else "error",
                    duration_ms=t["ms"], payload={**err.to_dict(), "attempt": attempt},
                )
                if not err.retryable or attempt >= max_attempt:
                    _fail_stage(conn, task_id, stage, err)
                    return TaskStatus.FAILED if err.code is not ErrCode.E_BUDGET else TaskStatus.PARTIAL
                backoff = settings.stage_backoff_sec[min(attempt - 1, len(settings.stage_backoff_sec) - 1)]
                time.sleep(backoff)
                continue
            except Exception as exc:  # 未归类异常：不重试，直接失败并留痕
                err = AppError(ErrCode.E_INTERNAL, f"unhandled: {exc!r}", retryable=False)
                _fail_stage(conn, task_id, stage, err)
                return TaskStatus.FAILED

        conn.execute(
            "UPDATE stages SET status = ?, idem_key = ?, artifact_id = ?, finished_at = ?"
            " WHERE task_id = ? AND stage = ?",
            (StageStatus.DONE.value, key, artifact_id, iso(), task_id, stage.value),
        )
        emit(
            conn, task_id, "stage_done", stage=stage.value, duration_ms=t["ms"],
            tokens=result.tokens, cost_cents=result.cost_cents,
            payload={"artifact": rel_path, "attempt": attempt, "meta": result.meta},
        )
        if result.cost_cents:
            q.add_budget(task_id, result.cost_cents)

        stage_budget = STAGE_BUDGET_CENTS[stage]
        if result.cost_cents > stage_budget:
            emit(conn, task_id, "retry", stage=stage.value, level="warn",
                 payload={"warn": "stage_budget_exceeded", "cost": result.cost_cents, "budget": stage_budget})

        _advance(conn, task_id, stage, q)
        return None

    return TaskStatus.FAILED


# --------------------------------------------------------------------- 任务
def run_task(conn: sqlite3.Connection, task_id: str, worker_id: str, settings: Settings) -> str:
    """把一个任务推进到终态。租约丢失时立即退出，交给下一个 worker。"""
    q = Queue(conn, settings)
    while True:
        task = get_task(conn, task_id)

        if task["status"] != TaskStatus.RUNNING.value or task["worker_id"] != worker_id:
            emit(conn, task_id, "lease", level="warn",
                 payload={"event": "lease_lost", "worker": worker_id})
            return str(task["status"])

        current = task["current_stage"]
        if not current:
            q.finish(task_id, TaskStatus.DONE)
            return TaskStatus.DONE.value

        terminal = run_one_stage(conn, task, Stage(current), settings, q)
        if terminal is not None:
            q.finish(task_id, terminal, error=f"terminal at {current}")
            return terminal.value
