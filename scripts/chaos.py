"""故障注入：三种场景，一条命令跑完。

    python scripts/chaos.py --scenario kill      # kill -9 持有者，另一个 worker 接手
    python scripts/chaos.py --scenario http500   # 上游持续 500，验证重试与死信
    python scripts/chaos.py --scenario timeout   # 上游超时，验证 E_TOOL_TIMEOUT
    python scripts/chaos.py --all

这个脚本是 M4 的验收证据，也是面试现场最值得演的那 90 秒。
所有场景都用真实进程 + 真实信号，不做 mock。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.queue import Queue
from app.core.state_machine import TaskStatus
from app.db import connect, init_db
from app.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


# --------------------------------------------------------------------- 工具
def _env(settings: Settings, extra: dict[str, str] | None = None) -> dict[str, str]:
    """故障注入用独立的库和产物目录，绝不碰演示数据。"""
    return {
        "PYTHONIOENCODING": "utf-8",
        "DB_PATH": settings.db_path,
        "ARTIFACTS_DIR": settings.artifacts_dir,
        **(extra or {}),
    }


def spawn_worker(name: str, settings: Settings, extra_env: dict[str, str] | None = None) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-m", "app.worker", "--name", name],
        cwd=str(ROOT), env={**os.environ, **_env(settings, extra_env)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def hard_kill(proc: subprocess.Popen) -> None:
    """Windows 上 proc.kill() 等价 TerminateProcess，即 kill -9。"""
    stop(proc)


def wait_for(conn, task_id: str, predicate, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is not None and predicate(dict(row)):
            return True
        time.sleep(interval)
    return False


def reset_db(settings: Settings) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(settings.db_path + suffix)
        for _ in range(10):
            try:
                path.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.3)


# --------------------------------------------------------------------- kill
def scenario_kill(settings: Settings) -> bool:
    print("\n=== [1/3] kill -9 持有者 ===")
    reset_db(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    q = Queue(conn, settings)
    task_id = q.enqueue("故障注入：kill 测试")
    print(f"  任务 {task_id} 已入队")

    workers = {"w1": spawn_worker("w1", settings), "w2": spawn_worker("w2", settings)}
    print("  已启动 w1 / w2")

    running = wait_for(
        conn, task_id,
        lambda t: t["status"] == "RUNNING" and t["current_stage"] in {"VOICE", "RENDER"},
        timeout=90, interval=0.1,
    )
    if not running:
        print("  [FAIL] 任务没有进入 VOICE/RENDER 阶段，无法验证续跑")
        return False

    holder = conn.execute("SELECT worker_id, current_stage FROM tasks WHERE id=?", (task_id,)).fetchone()
    name = holder["worker_id"]
    print(f"  当前持有者 {name}（阶段 {holder['current_stage']}），执行 kill -9")
    hard_kill(workers[name])
    print(f"  {name} 已被杀死")

    drained = wait_for(conn, task_id, lambda t: t["status"] in {"PENDING", "RUNNING"} and t["worker_id"] != name, timeout=90)
    final = wait_for(conn, task_id, lambda t: t["status"] in {"DONE", "PARTIAL", "FAILED", "NEEDS_HUMAN"}, timeout=180)

    row = conn.execute("SELECT status, attempt, worker_id, error FROM tasks WHERE id=?", (task_id,)).fetchone()
    versions = conn.execute(
        "SELECT stage, COUNT(*) c FROM artifacts WHERE task_id=? GROUP BY stage HAVING c > 1", (task_id,)
    ).fetchall()
    total_artifacts = conn.execute("SELECT COUNT(*) c FROM artifacts WHERE task_id=?", (task_id,)).fetchone()["c"]
    research_runs = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND type='stage_done' AND stage='RESEARCH'", (task_id,)
    ).fetchone()["c"]

    print(f"  接手={drained} 终态={final} status={row['status']} leases={row['attempt']}")
    print(f"  产物={total_artifacts}（应为 7） 重复生成的阶段: {[v['stage'] for v in versions] or '无'}")
    print(f"  RESEARCH 执行次数={research_runs}（应为 1，说明没有从头重算）")

    for proc in workers.values():
        stop(proc)

    ok = (
        row["status"] in {"DONE", "PARTIAL"}
        and row["attempt"] >= 2
        and not versions
        and research_runs == 1
    )
    print(f"  [{'PASS' if ok else 'FAIL'}] 被杀后由另一个 worker 从断点接手，已完成阶段零重算")
    conn.close()
    return ok


# --------------------------------------------------------------------- 假上游
class _Stub(BaseHTTPRequestHandler):
    mode = "500"

    def do_POST(self) -> None:  # noqa: N802
        if _Stub.mode == "timeout":
            time.sleep(6)
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        body = json.dumps({"error": {"message": "injected failure"}}).encode()
        self.send_response(500)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # 客户端超时先断了，正常现象

    def log_message(self, *args) -> None:  # 静音
        return


def _serve(mode: str, port: int) -> ThreadingHTTPServer:
    _Stub.mode = mode
    server = ThreadingHTTPServer(("127.0.0.1", port), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _upstream_scenario(settings: Settings, mode: str, port: int, expect: str) -> bool:
    print(f"\n=== 场景：上游{'超时' if mode == 'timeout' else '持续 500'} ===")
    reset_db(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    q = Queue(conn, settings)
    task_id = q.enqueue("故障注入：上游异常")
    server = _serve(mode, port)

    env = {
        "LLM_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "LLM_API_KEY": "test",
        "LLM_MODEL": "stub",
        "LLM_TIMEOUT_SEC": "2",
        "STAGE_BACKOFF_SEC": "[0,0,0]",
        "MAX_ATTEMPT": "3",
    }
    proc = spawn_worker("wf", settings, env)
    finished = wait_for(conn, task_id, lambda t: t["status"] in {"FAILED", "PARTIAL"}, timeout=90)
    stop(proc)
    server.shutdown()

    row = conn.execute("SELECT status, attempt, error FROM tasks WHERE id=?", (task_id,)).fetchone()
    err = str(row["error"] or "")
    ok = finished and row["status"] == "FAILED" and expect in err
    print(f"  终态={row['status']} leases={row['attempt']} error={err[:90]}")
    print(f"  [{'PASS' if ok else 'FAIL'}] 异常被归类为 {expect}，任务进入死信而不是无限重试")
    conn.close()
    return ok


# --------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["kill", "http500", "timeout", "all"], default="all")
    ap.add_argument("--all", action="store_true", help="等价于 --scenario all")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    if args.all:
        args.scenario = "all"

    settings = Settings().model_copy(
        update={"db_path": "chaos.db", "artifacts_dir": "artifacts_chaos"}
    )
    results: list[tuple[str, bool]] = []

    if args.scenario in {"kill", "all"}:
        results.append(("kill", scenario_kill(settings)))
    if args.scenario in {"http500", "all"}:
        results.append(("http500", _upstream_scenario(settings, "500", args.port, "E_TOOL_UPSTREAM")))
    if args.scenario in {"timeout", "all"}:
        results.append(("timeout", _upstream_scenario(settings, "timeout", args.port + 1, "E_TOOL_TIMEOUT")))

    print("\n=== 汇总 ===")
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
