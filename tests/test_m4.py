"""M4 分布式加固：断点续跑、死信闸、租约回收事件。

进程级 kill 由 `scripts/chaos.py` 覆盖（真实 TerminateProcess），
这里用「租约过期」来构造同一语义，保证单测快且确定。
"""

from __future__ import annotations

import time
from pathlib import Path

from app.core.queue import Queue
from app.core.runner import run_task
from app.core.state_machine import StageStatus, TaskStatus


def _done_task(conn, settings) -> tuple[str, Queue]:
    q = Queue(conn, settings)
    tid = q.enqueue("续跑测试")
    assert q.lease("w1") is not None
    assert run_task(conn, tid, "w1", settings) == TaskStatus.DONE.value
    return tid, q


def test_lease_expiry_resumes_without_recompute(conn, settings):
    """模拟 worker 在 RENDER 中途被杀：租约过期回收 → 新 worker 从断点续跑。

    真实场景里被杀的那个阶段还没写产物，所以续跑时它重跑一次、产出 v1；
    它之前的 6 个阶段一个都不能重算。
    """
    from app.core.runner import run_one_stage
    from app.core.state_machine import Stage

    q = Queue(conn, settings)
    tid = q.enqueue("续跑测试")
    task = q.lease("w1")
    assert task is not None

    # 先跑完 RESEARCH → VOICE，停在 RENDER 之前
    for stage in (Stage.RESEARCH, Stage.SCRIPT, Stage.STORYBOARD, Stage.ASSETS, Stage.VOICE):
        assert run_one_stage(conn, task, stage, settings, q) is None
    assert q.get(tid)["current_stage"] == "RENDER"

    before = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE task_id=?", (tid,)).fetchone()["c"]
    assert before == 5, "RESEARCH…VOICE 共 5 个阶段"

    # worker 被杀：状态仍是 RUNNING，但租约已过期
    conn.execute(
        "UPDATE tasks SET worker_id=?, lease_until=? WHERE id=?",
        ("dead-worker", time.time() - 5, tid),
    )

    reclaimed = q.reclaim()
    assert tid in reclaimed
    assert q.get(tid)["status"] == TaskStatus.PENDING.value
    assert q.get(tid)["current_stage"] == "RENDER", "断点必须保留在 RENDER"

    task2 = q.lease("w2")
    assert task2 is not None and task2["id"] == tid
    assert run_task(conn, tid, "w2", settings) == TaskStatus.DONE.value

    after = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE task_id=?", (tid,)).fetchone()["c"]
    assert after == before + 2, "续跑只补 RENDER 和 QC，前 5 个阶段不重算"

    research_done = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND type='stage_done' AND stage='RESEARCH'", (tid,)
    ).fetchone()["c"]
    assert research_done == 1, "已完成阶段不应重算"

    reclaimed_events = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND payload_json LIKE '%lease_expired_requeued%'", (tid,)
    ).fetchone()["c"]
    assert reclaimed_events == 1


def test_attempt_cap_dead_letters(conn, settings):
    """反复被抢（一只在崩的 worker）必须进死信，不能无限重试。"""
    q = Queue(conn, settings)
    tid = q.enqueue("死信测试")
    conn.execute("UPDATE tasks SET attempt = 99 WHERE id = ?", (tid,))
    task = q.lease("w-broken")
    assert task is not None

    assert run_task(conn, tid, "w-broken", settings) == TaskStatus.FAILED.value
    row = q.get(tid)
    assert row["status"] == TaskStatus.FAILED.value
    assert "E_LOOP" in str(row["error"])

    dead = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND payload_json LIKE '%dead_letter%'", (tid,)
    ).fetchone()["c"]
    assert dead == 1


def test_killed_stage_reruns_only_itself(conn, settings):
    """被杀时正在跑的阶段要重跑，之前的阶段一个都不能动。"""
    q = Queue(conn, settings)
    tid = q.enqueue("单阶段重跑")
    assert q.lease("w1") is not None

    # 只跑到 STORYBOARD：手工把 current_stage 推到这里，前面都已 DONE
    from app.core.runner import run_one_stage
    from app.core.state_machine import Stage

    task = q.get(tid)
    assert task is not None
    for stage in (Stage.RESEARCH, Stage.SCRIPT, Stage.STORYBOARD):
        assert run_one_stage(conn, task, stage, settings, q) is None

    counts = {
        s: conn.execute("SELECT COUNT(*) c FROM artifacts WHERE task_id=? AND stage=?", (tid, s)).fetchone()["c"]
        for s in ("RESEARCH", "SCRIPT", "STORYBOARD")
    }
    assert counts == {"RESEARCH": 1, "SCRIPT": 1, "STORYBOARD": 1}

    # 继续把剩下的跑完，前面三个阶段不应再产生产物
    assert run_task(conn, tid, "w1", settings) == TaskStatus.DONE.value
    for stage, expected in counts.items():
        actual = conn.execute(
            "SELECT COUNT(*) c FROM artifacts WHERE task_id=? AND stage=?", (tid, stage)
        ).fetchone()["c"]
        assert actual == expected


def test_artifact_paths_are_relative(conn, settings):
    tid, _ = _done_task(conn, settings)
    rows = conn.execute("SELECT path FROM artifacts WHERE task_id=?", (tid,)).fetchall()
    for row in rows:
        value = row["path"]
        assert not Path(value).is_absolute(), f"artifacts 表应存相对路径: {value}"
        assert (Path(settings.artifacts_dir) / value).exists()
