"""Worker 进程入口。

    python -m app.worker --name w1
    python -m app.worker --name w2          # 第二个终端，演示并发抢任务
    python -m app.worker --once            # 处理一个任务就退出（冒烟测试用）

职责：抢任务 → 起心跳线程 → 推进到终态 → 释放。
心跳必须用**独立连接**：SQLite 连接不保证线程安全，跨线程并发写会出错。
"""

from __future__ import annotations

import argparse
import threading
import time

from app.core.queue import Queue
from app.core.runner import run_task
from app.core.state_machine import TaskStatus
from app.core.trace import emit
from app.db import connect, init_db
from app.settings import Settings


def _heartbeat_loop(task_id: str, worker_id: str, settings: Settings, stop: threading.Event) -> None:
    conn = connect(settings.db_path)
    q = Queue(conn, settings)
    try:
        while not stop.wait(settings.heartbeat_sec):
            if not q.heartbeat(task_id, worker_id):
                emit(conn, task_id, "lease", level="warn",
                     payload={"event": "heartbeat_failed", "worker": worker_id})
                return
    finally:
        conn.close()


def run_worker(name: str, settings: Settings, once: bool = False) -> None:
    conn = connect(settings.db_path)
    init_db(conn)
    q = Queue(conn, settings)
    last_reclaim = 0.0
    print(f"[worker:{name}] started (lease={settings.lease_sec}s, poll={settings.poll_sec}s)")

    while True:
        if time.time() - last_reclaim >= settings.reclaim_sec:
            reclaimed = q.reclaim()
            if reclaimed:
                print(f"[worker:{name}] reclaimed {len(reclaimed)} expired task(s): {reclaimed}")
            last_reclaim = time.time()

        task = q.lease(name)
        if task is None:
            if once:
                break
            time.sleep(settings.poll_sec)
            continue

        task_id = str(task["id"])
        print(f"[worker:{name}] leased {task_id} topic={task['topic']!r}")
        stop = threading.Event()
        hb = threading.Thread(target=_heartbeat_loop, args=(task_id, name, settings, stop), daemon=True)
        hb.start()
        try:
            final = run_task(conn, task_id, name, settings)
            print(f"[worker:{name}] {task_id} -> {final}")
        except Exception as exc:  # worker 最外层唯一的兜底
            q.finish(task_id, TaskStatus.FAILED, error=repr(exc))
            emit(conn, task_id, "validation_fail", level="error", payload={"fatal": repr(exc)})
            print(f"[worker:{name}] {task_id} crashed: {exc!r}")
        finally:
            stop.set()
            hb.join(timeout=2)
        if once:
            break

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="video agent worker")
    parser.add_argument("--name", default="w1")
    parser.add_argument("--once", action="store_true", help="处理一个任务后退出")
    args = parser.parse_args()
    run_worker(args.name, Settings(), once=args.once)


if __name__ == "__main__":
    main()
