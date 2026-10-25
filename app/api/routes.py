"""HTTP 接口：提交任务、查状态、查事件、健康检查。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.queue import Queue
from app.core.state_machine import StageStatus

router = APIRouter()


class TaskCreate(BaseModel):
    topic: str = Field(min_length=1, max_length=200, description="视频主题")
    partition: str | None = Field(default=None, max_length=20, description="B站分区，可选")


class TaskCreated(BaseModel):
    task_id: str
    status: str
    deduplicated: bool = False


def _queue(request: Request) -> Queue:
    return Queue(request.app.state.conn, request.app.state.settings)


@router.get("/healthz")
def healthz(request: Request) -> dict[str, Any]:
    conn = request.app.state.conn
    conn.execute("SELECT 1").fetchone()
    return {"ok": True, "version": request.app.version}


@router.post("/tasks", status_code=202, response_model=TaskCreated)
def create_task(
    payload: TaskCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> TaskCreated:
    q = _queue(request)
    before = q.conn.execute(
        "SELECT id FROM tasks WHERE idem_submit = ?", (idempotency_key,)
    ).fetchone() if idempotency_key else None
    task_id = q.enqueue(payload.topic.strip(), payload.partition, idempotency_key)
    return TaskCreated(task_id=task_id, status="PENDING", deduplicated=before is not None)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict[str, Any]:
    q = _queue(request)
    task = q.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    stages = q.stages(task_id)
    done = sum(1 for s in stages if s["status"] in (StageStatus.DONE.value, StageStatus.SKIPPED.value))
    return {
        "task": {
            "id": task["id"],
            "topic": task["topic"],
            "status": task["status"],
            "current_stage": task["current_stage"],
            "attempt": task["attempt"],
            "worker_id": task["worker_id"],
            "budget_spent_cents": task["budget_spent_cents"],
            "error": task["error"],
            "created_at": task["created_at"],
            "updated_at": task["updated_at"],
        },
        "stages": [
            {
                "stage": s["stage"],
                "status": s["status"],
                "attempt": s["attempt"],
                "duration_ms": None,
                "error": s["error"],
            }
            for s in stages
        ],
        "progress": {"done": done, "total": len(stages)},
    }


@router.get("/tasks/{task_id}/events")
def get_events(
    task_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
    after_id: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = request.app.state.conn.execute(
        "SELECT id, ts, level, stage, type, duration_ms, tokens, cost_cents, payload_json"
        " FROM events WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (task_id, after_id, limit),
    ).fetchall()
    return {"events": [dict(r) for r in rows]}


@router.get("/tasks")
def list_tasks(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    rows = request.app.state.conn.execute(
        "SELECT id, topic, status, current_stage, created_at FROM tasks"
        " ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"tasks": [dict(r) for r in rows]}
