"""两个 worker 进程并发处理任务：验证原子抢锁与分摊（进程级）。

这是 M0 验收里"并发安全"的硬证据，也是 M4 故障注入测试的地基。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from app.core.queue import Queue
from app.core.state_machine import TERMINAL_TASK_STATUSES, TaskStatus
from app.db import connect, init_db
from app.settings import Settings


def test_two_processes_do_not_double_execute(tmp_path):
    settings = Settings(
        _env_file=None,
        db_path=str(tmp_path / "t.db"),
        artifacts_dir=str(tmp_path / "art"),
        fake_stage_sleep=0.02,
        poll_sec=0.1,
    )
    conn = connect(settings.db_path)
    init_db(conn)
    q = Queue(conn, settings)
    ids = [q.enqueue(f"并发 {i}") for i in range(4)]

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        # 子进程必须指向同一个临时库，否则它们会去写 ./app.db
        "DB_PATH": settings.db_path,
        "ARTIFACTS_DIR": settings.artifacts_dir,
        "FAKE_STAGE_SLEEP": "0.02",
        "POLL_SEC": "0.1",
    }
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "app.worker", "--name", f"w{i + 1}"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(2)
    ]

    deadline = time.time() + 90
    terminal = {s.value for s in TERMINAL_TASK_STATUSES}
    try:
        while time.time() < deadline:
            rows = conn.execute(
                f"SELECT status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
            if all(r["status"] in terminal for r in rows):
                break
            time.sleep(0.2)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    for tid in ids:
        task = q.get(tid)
        assert task is not None, f"missing task {tid}"
        assert task["status"] == TaskStatus.DONE.value, f"{tid} -> {task['status']}"
        assert task["attempt"] == 1, f"{tid} 被抢了 {task['attempt']} 次，原子性失效"
    conn.close()
