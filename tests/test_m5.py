"""M5 质检与回退：定位坏镜头、只重做它、VOICE 靠幂等复用。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PIL import Image

from app.core.queue import Queue
from app.core.runner import run_task
from app.core.state_machine import TaskStatus

SHOT_BAD = 1


def _run_to_done(conn, settings, topic: str = "质检回退测试") -> str:
    q = Queue(conn, settings)
    tid = q.enqueue(topic)
    assert q.lease("w1") is not None
    assert run_task(conn, tid, "w1", settings) == TaskStatus.DONE.value
    return tid


def _restart_at(conn, tid: str, stage: str) -> None:
    """把任务重新挂到某个阶段上，模拟"再来一次质检"。"""
    conn.execute(
        "UPDATE tasks SET status=?, current_stage=?, worker_id=?, lease_until=? WHERE id=?",
        (TaskStatus.RUNNING.value, stage, "w2", time.time() + 120, tid),
    )
    conn.execute("UPDATE stages SET status='PENDING' WHERE task_id=? AND stage=?", (tid, stage))


def _artifact(conn, settings, tid: str, stage: str, version: int | None = None) -> dict:
    sql = "SELECT path FROM artifacts WHERE task_id=? AND stage=?"
    params: list[object] = [tid, stage]
    if version is not None:
        sql += " AND version=?"
        params.append(version)
    sql += " ORDER BY version DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return json.loads((Path(settings.artifacts_dir) / row["path"]).read_text(encoding="utf-8"))


def test_qc_retries_bad_shot_and_reuses_voice(conn, settings):
    tid = _run_to_done(conn, settings)
    before_assets = _artifact(conn, settings, tid, "ASSETS")

    # 弄坏第 2 镜（纯黑 → QC 的 flat 检测），然后重新触发质检
    bad_path = Path(settings.artifacts_dir) / before_assets["shots"][SHOT_BAD]["path"]
    Image.new("RGB", (960, 540), (0, 0, 0)).save(bad_path)
    _restart_at(conn, tid, "QC")

    assert run_task(conn, tid, "w2", settings) == TaskStatus.DONE.value

    # 1) ASSETS 跑了两遍
    assert conn.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE task_id=? AND stage='ASSETS'", (tid,)
    ).fetchone()["c"] == 2

    # 2) 只有坏镜头被标记重做（shots 表是证据）
    rows = {
        r["idx"]: dict(r)
        for r in conn.execute("SELECT idx, status, retry_count FROM shots WHERE task_id=?", (tid,))
    }
    assert rows[SHOT_BAD]["retry_count"] == 1
    assert all(rows[i]["retry_count"] == 0 for i in rows if i != SHOT_BAD)
    assert all(rows[i]["status"] == "DONE" for i in rows)

    # 3) VOICE 只跑过一次，第二次 SKIPPED
    voice_done = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND type='stage_done' AND stage='VOICE'", (tid,)
    ).fetchone()["c"]
    voice_skip = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND type='stage_skip' AND stage='VOICE'", (tid,)
    ).fetchone()["c"]
    assert voice_done == 1, "配音不能因为换素材而重跑"
    assert voice_skip >= 1, "VOICE 应命中幂等键被跳过"

    # 4) RENDER 重做一次；QC 跑了三次（初检 + 回退后复检 + 触发的那次）
    assert conn.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE task_id=? AND stage='RENDER'", (tid,)
    ).fetchone()["c"] == 2
    assert conn.execute(
        "SELECT COUNT(*) c FROM artifacts WHERE task_id=? AND stage='QC'", (tid,)
    ).fetchone()["c"] == 3

    # 5) 回退事件与轮次
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id=? AND payload_json LIKE '%rollback%'", (tid,)
    ).fetchone()["c"] == 1
    assert conn.execute("SELECT qc_rounds FROM tasks WHERE id=?", (tid,)).fetchone()["qc_rounds"] == 1


def test_qc_partial_when_duration_out_of_range(conn, settings):
    strict = settings.model_copy(update={"qc_min_sec": 45.0, "qc_max_sec": 90.0})
    tid = _run_to_done(conn, strict, topic="时长越界")

    row = conn.execute("SELECT path FROM artifacts WHERE task_id=? AND stage='RENDER'", (tid,)).fetchone()
    path = Path(strict.artifacts_dir) / row["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["duration_sec"] = 12.0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    _restart_at(conn, tid, "QC")
    assert run_task(conn, tid, "w2", strict) == TaskStatus.PARTIAL.value

    task = conn.execute("SELECT status, error FROM tasks WHERE id=?", (tid,)).fetchone()
    assert task["status"] == TaskStatus.PARTIAL.value
    assert "duration_in_range" in str(task["error"])


def test_qc_rounds_exhausted_goes_partial(conn, settings):
    strict = settings.model_copy(update={"qc_max_rounds": 0})
    tid = _run_to_done(conn, strict, topic="轮次用尽")

    assets = _artifact(conn, strict, tid, "ASSETS")
    Image.new("RGB", (960, 540), (0, 0, 0)).save(
        Path(strict.artifacts_dir) / assets["shots"][SHOT_BAD]["path"]
    )
    _restart_at(conn, tid, "QC")

    assert run_task(conn, tid, "w2", strict) == TaskStatus.PARTIAL.value
    report = _artifact(conn, strict, tid, "QC", version=2)
    assert report["decided"] == "PARTIAL"
    assert "shot_quality" in str(report["degraded_reason"])
