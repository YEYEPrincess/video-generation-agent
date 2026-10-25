"""Runner：端到端跑通、幂等复用、预算闸。"""

from __future__ import annotations

from pathlib import Path

from app.core.queue import Queue
from app.core.runner import run_task
from app.core.state_machine import StageStatus, TaskStatus


def _lease(q: Queue, worker: str = "w1") -> dict:
    task = q.lease(worker)
    assert task is not None
    return task


def test_full_pipeline_reaches_done(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    _lease(q)
    assert run_task(conn, tid, "w1", settings) == TaskStatus.DONE.value

    task = q.get(tid)
    assert task["status"] == TaskStatus.DONE.value
    assert task["current_stage"] is None

    stages = q.stages(tid)
    assert all(s["status"] == StageStatus.DONE.value for s in stages)
    assert [s["stage"] for s in stages] == [
        "RESEARCH", "SCRIPT", "STORYBOARD", "ASSETS", "VOICE", "RENDER", "QC"
    ]


def test_artifacts_written_for_every_stage(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    _lease(q)
    run_task(conn, tid, "w1", settings)

    rows = conn.execute("SELECT * FROM artifacts WHERE task_id = ?", (tid,)).fetchall()
    assert len(rows) == 7
    for r in rows:
        path = Path(settings.artifacts_dir) / r["path"]
        assert path.exists(), f"missing artifact {path}"
        assert path.read_text(encoding="utf-8").startswith("{")


def test_events_emitted(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    _lease(q)
    run_task(conn, tid, "w1", settings)

    rows = conn.execute("SELECT type FROM events WHERE task_id = ?", (tid,)).fetchall()
    types = [r["type"] for r in rows]
    assert types.count("stage_start") == 7
    assert types.count("stage_done") == 7
    assert types.count("state_transition") >= 8  # PENDING + 6 次推进 + DONE


def test_rerun_skips_completed_stages(conn, settings):
    """模拟 worker 崩溃后重跑：已完成阶段必须 SKIPPED，且不重写产物。"""
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    _lease(q)
    run_task(conn, tid, "w1", settings)

    before = conn.execute("SELECT COUNT(*) AS c FROM artifacts WHERE task_id = ?", (tid,)).fetchone()["c"]
    conn.execute(
        "UPDATE tasks SET status = ?, current_stage = ?, worker_id = ?, lease_until = ? WHERE id = ?",
        (TaskStatus.RUNNING.value, "RESEARCH", "w1", 9e9, tid),
    )
    assert run_task(conn, tid, "w1", settings) == TaskStatus.DONE.value

    after = conn.execute("SELECT COUNT(*) AS c FROM artifacts WHERE task_id = ?", (tid,)).fetchone()["c"]
    assert after == before, "幂等失效：已完成阶段被重新生成"

    skips = conn.execute(
        "SELECT COUNT(*) AS c FROM events WHERE task_id = ? AND type = 'stage_skip'", (tid,)
    ).fetchone()["c"]
    assert skips == 7


def test_budget_exhausted_returns_partial(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    _lease(q)
    broke = settings.model_copy(update={"budget_cents": 0})
    assert run_task(conn, tid, "w1", broke) == TaskStatus.PARTIAL.value
    assert q.get(tid)["status"] == TaskStatus.PARTIAL.value


def test_lease_lost_stops_execution(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    _lease(q, "w1")
    conn.execute("UPDATE tasks SET worker_id = 'w2' WHERE id = ?", (tid,))
    assert run_task(conn, tid, "w1", settings) == TaskStatus.RUNNING.value
