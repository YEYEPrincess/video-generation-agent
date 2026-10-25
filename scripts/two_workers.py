"""起 N 个 worker 进程，验证：任务不被重复执行、被分摊到不同进程。

    python scripts/two_workers.py --workers 2 --tasks 3

M0：证明"原子抢锁"在**进程级**成立。
M4：同一脚本加 `--chaos kill`，验证 kill 之后被另一个 worker 接手。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 允许 `python scripts/two_workers.py` 直接运行（而不必 -m）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.queue import Queue
from app.core.state_machine import TERMINAL_TASK_STATUSES, TaskStatus
from app.db import connect, init_db
from app.settings import Settings


def _lease_workers(conn) -> set[str]:
    """从事件里还原"谁抢过任务"——tasks.worker_id 在终态会被清空。"""
    rows = conn.execute("SELECT payload_json FROM events WHERE type = 'lease'").fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if payload.get("worker"):
            out.add(str(payload["worker"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--tasks", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=40.0)
    args = ap.parse_args()

    settings = Settings()
    conn = connect(settings.db_path)
    init_db(conn)
    q = Queue(conn, settings)

    ids = [q.enqueue(f"并发验证任务 {i}") for i in range(args.tasks)]
    print(f"[test] enqueued {len(ids)} tasks")

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs = [
        subprocess.Popen([sys.executable, "-m", "app.worker", "--name", f"w{i + 1}"], env=env)
        for i in range(args.workers)
    ]
    print(f"[test] started {len(procs)} workers")

    deadline = time.time() + args.seconds
    terminal = {s.value for s in TERMINAL_TASK_STATUSES}
    try:
        while time.time() < deadline:
            rows = conn.execute(
                f"SELECT status FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
            if all(r["status"] in terminal for r in rows):
                break
            time.sleep(0.3)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    print("\n=== result ===")
    for tid in ids:
        t = q.get(tid)
        assert t is not None
        print(f"  {tid}  {t['status']:<7} leases={t['attempt']}  error={t['error'] or '-'}")

    workers = _lease_workers(conn)
    checks = {
        "all DONE": all((q.get(t) or {}).get("status") == TaskStatus.DONE.value for t in ids),
        "no duplicate execution (每任务仅被抢 1 次)": all((q.get(t) or {}).get("attempt") == 1 for t in ids),
        f"work distributed (实际参与者: {sorted(workers)})": len(workers) >= min(2, args.workers, args.tasks),
    }
    print()
    for name, passed in checks.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    conn.close()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
