"""任务队列：enqueue / lease / heartbeat / reclaim / finish。

"分布式"的全部秘密就在这四个方法里：
- 原子抢锁：单条 UPDATE + 子查询 + 状态回验，同一任务只会被一个 worker 抢到；
- 租约：抢到就写 `lease_until`，靠心跳续期；
- 回收：租约过期 = 持有者已死 → 任务回到 PENDING，另一个 worker 接手。
"""

from __future__ import annotations

import sqlite3
import uuid

from app.core.state_machine import STAGE_ORDER, StageStatus, TaskStatus
from app.core.trace import emit
from app.db import iso, now, tx
from app.settings import Settings


class Queue:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.s = settings

    # ------------------------------------------------------------------ 入队
    def enqueue(self, topic: str, partition: str | None = None, idem_submit: str | None = None) -> str:
        """创建任务 + 7 行阶段记录。幂等键命中直接返回已有 task_id。"""
        if idem_submit:
            row = self.conn.execute("SELECT id FROM tasks WHERE idem_submit = ?", (idem_submit,)).fetchone()
            if row:
                return str(row["id"])

        task_id = "t_" + uuid.uuid4().hex[:12]
        ts = iso()
        try:
            with tx(self.conn):
                self.conn.execute(
                    "INSERT INTO tasks(id, topic, partition, status, current_stage, idem_submit,"
                    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        topic,
                        partition,
                        TaskStatus.PENDING.value,
                        STAGE_ORDER[0].value,
                        idem_submit,
                        ts,
                        ts,
                    ),
                )
                self.conn.executemany(
                    "INSERT INTO stages(task_id, stage, status) VALUES (?,?,?)",
                    [(task_id, s.value, StageStatus.PENDING.value) for s in STAGE_ORDER],
                )
        except sqlite3.IntegrityError:
            # 并发提交同一个 Idempotency-Key：让先到的那次赢
            row = self.conn.execute("SELECT id FROM tasks WHERE idem_submit = ?", (idem_submit,)).fetchone()
            if row:
                return str(row["id"])
            raise

        emit(self.conn, task_id, "state_transition", payload={"to": TaskStatus.PENDING.value, "topic": topic})
        return task_id

    # ------------------------------------------------------------------ 抢锁
    def lease(self, worker_id: str) -> dict | None:
        t = now()
        row = self.conn.execute(
            """
            UPDATE tasks
               SET status = ?, worker_id = ?, lease_until = ?, attempt = attempt + 1, updated_at = ?
             WHERE id = (SELECT id FROM tasks WHERE status = ? ORDER BY created_at ASC, id ASC LIMIT 1)
               AND status = ?
            RETURNING *
            """,
            (
                TaskStatus.RUNNING.value,
                worker_id,
                t + self.s.lease_sec,
                iso(t),
                TaskStatus.PENDING.value,
                TaskStatus.PENDING.value,
            ),
        ).fetchone()
        if row is None:
            return None
        emit(
            self.conn,
            str(row["id"]),
            "lease",
            payload={"event": "acquired", "worker": worker_id, "attempt": row["attempt"]},
        )
        return dict(row)

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        """续租。返回 False 表示租约已经不属于自己（被回收或被别人抢走）。"""
        cur = self.conn.execute(
            "UPDATE tasks SET lease_until = ?, updated_at = ?"
            " WHERE id = ? AND worker_id = ? AND status = ?",
            (now() + self.s.lease_sec, iso(), task_id, worker_id, TaskStatus.RUNNING.value),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ 回收
    def reclaim(self) -> list[str]:
        """把租约过期的 RUNNING 任务打回 PENDING。返回被回收的 task_id 列表。"""
        rows = self.conn.execute(
            """
            UPDATE tasks
               SET status = ?, worker_id = NULL, lease_until = NULL, updated_at = ?
             WHERE status = ? AND lease_until IS NOT NULL AND lease_until < ?
            RETURNING id, attempt
            """,
            (TaskStatus.PENDING.value, iso(), TaskStatus.RUNNING.value, now()),
        ).fetchall()
        ids = [str(r["id"]) for r in rows]
        for r in rows:
            emit(
                self.conn,
                str(r["id"]),
                "state_transition",
                level="warn",
                payload={"event": "lease_expired_requeued", "attempt": r["attempt"]},
            )
        return ids

    # ------------------------------------------------------------------ 终态
    def finish(self, task_id: str, status: TaskStatus, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = ?, error = ?, worker_id = NULL, lease_until = NULL, updated_at = ?"
            " WHERE id = ?",
            (status.value, error, iso(), task_id),
        )
        emit(self.conn, task_id, "state_transition", level="warn" if status is not TaskStatus.DONE else "info",
             payload={"to": status.value, "error": error})

    # ------------------------------------------------------------------ 查询
    def get(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def stages(self, task_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM stages WHERE task_id = ? ORDER BY rowid", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def add_budget(self, task_id: str, cents: int) -> int:
        self.conn.execute(
            "UPDATE tasks SET budget_spent_cents = budget_spent_cents + ?, updated_at = ? WHERE id = ?",
            (cents, iso(), task_id),
        )
        row = self.conn.execute("SELECT budget_spent_cents FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return int(row["budget_spent_cents"]) if row else 0
