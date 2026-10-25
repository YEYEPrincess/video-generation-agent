"""队列与租约：原子性、幂等、回收。"""

from __future__ import annotations

import threading
import time

from app.core.queue import Queue
from app.core.state_machine import StageStatus, TaskStatus
from app.db import connect, init_db


def test_enqueue_creates_task_and_seven_stages(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("测试主题")
    task = q.get(tid)
    assert task is not None
    assert task["status"] == TaskStatus.PENDING.value
    stages = q.stages(tid)
    assert len(stages) == 7
    assert [s["stage"] for s in stages][0] == "RESEARCH"
    assert all(s["status"] == StageStatus.PENDING.value for s in stages)


def test_idempotent_submit_returns_same_task(conn, settings):
    q = Queue(conn, settings)
    a = q.enqueue("同一个主题", idem_submit="key-1")
    b = q.enqueue("同一个主题", idem_submit="key-1")
    assert a == b
    assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1


def test_lease_once_then_none(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("t")
    first = q.lease("w1")
    assert first is not None and first["id"] == tid
    assert first["worker_id"] == "w1"
    assert first["lease_until"] > time.time()
    assert q.lease("w2") is None


def test_heartbeat_and_finish(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("t")
    q.lease("w1")
    before = q.get(tid)["lease_until"]
    time.sleep(0.05)  # Windows 时钟粒度约 15ms，不睡会拿到同一个 tick
    assert q.heartbeat(tid, "w1") is True
    assert q.get(tid)["lease_until"] >= before
    assert q.heartbeat(tid, "w2") is False
    q.finish(tid, TaskStatus.DONE)
    assert q.heartbeat(tid, "w1") is False


def test_reclaim_expired_lease(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("t")
    q.lease("w1")
    conn.execute("UPDATE tasks SET lease_until = ? WHERE id = ?", (time.time() - 10, tid))
    assert q.reclaim() == [tid]
    task = q.get(tid)
    assert task["status"] == TaskStatus.PENDING.value
    assert task["worker_id"] is None
    assert q.reclaim() == []


def test_lease_is_atomic_under_concurrency(settings):
    """4 个线程抢 5 个任务：必须每个任务恰好被抢到一次。"""
    bootstrap = connect(settings.db_path)
    init_db(bootstrap)
    q0 = Queue(bootstrap, settings)
    ids = [q0.enqueue(f"topic-{i}") for i in range(5)]
    bootstrap.close()

    seen: list[str] = []
    lock = threading.Lock()

    def worker(wid: str) -> None:
        c = connect(settings.db_path)
        q = Queue(c, settings)
        try:
            while True:
                task = q.lease(wid)
                if task is None:
                    return
                with lock:
                    seen.append(str(task["id"]))
                time.sleep(0.01)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len(seen) == 5, f"expected 5 leases, got {len(seen)}: {seen}"
    assert set(seen) == set(ids)
    assert len(set(seen)) == 5, "任务被重复抢到，原子性失效"


def test_budget_accumulates(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("t")
    assert q.add_budget(tid, 3) == 3
    assert q.add_budget(tid, 4) == 7
