"""评测：20 条主题跑完整链路，产出指标表。

    python eval/run_eval.py --limit 20 --draft

指标口径（写进 README，避免自欺）：
- 成功率：DONE + PARTIAL（有产物）= 可交付；strict = 只有 DONE
- 时长合规：45 ≤ ffprobe 时长 ≤ 90
- motion_score：首镜内两帧的灰度平均差（0 = 静止画面），阈值 1.0
- 延迟：单条任务墙钟时间；分阶段耗时从 events 表取
- 成本：events.cost_cents 之和（Mock 模型下为 0，会如实标注）
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageChops, ImageStat  # noqa: E402

from app.contracts import STAGE_CONTRACTS  # noqa: E402
from app.core.queue import Queue  # noqa: E402
from app.core.runner import run_task  # noqa: E402
from app.core.state_machine import Stage  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.settings import Settings  # noqa: E402


def load_golden(path: Path, limit: int, offset: int = 0) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[offset : offset + limit]


def draft_settings(base: Settings | None = None, db_path: str = "eval.db",
                   artifacts_dir: str = "artifacts_eval") -> Settings:
    s = base or Settings()
    return s.model_copy(
        update={
            "render_width": 1280,
            "render_height": 720,
            "render_fps": 24,
            "render_preset": "ultrafast",
            "artifacts_dir": artifacts_dir,
            "db_path": db_path,
        }
    )


def artifact(conn, settings, task_id: str, stage: Stage) -> dict:
    row = conn.execute(
        "SELECT path FROM artifacts WHERE task_id=? AND stage=? ORDER BY version DESC LIMIT 1",
        (task_id, stage.value),
    ).fetchone()
    if row is None:
        return {}
    return json.loads((Path(settings.artifacts_dir) / row["path"]).read_text(encoding="utf-8"))


def frame(video: Path, at: float, out: Path) -> Image.Image:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at),
         "-i", str(video), "-frames:v", "1", str(out)],
        check=True, capture_output=True,
    )
    return Image.open(out).convert("L")


def motion_score(video: Path, duration: float, workdir: Path) -> float | None:
    """首镜内两帧的平均灰度差。用来抓"技术指标全绿但画面是死的"。"""
    try:
        a = frame(video, 0.5, workdir / "m_a.png")
        b = frame(video, min(5.0, max(1.2, duration * 0.4)), workdir / "m_b.png")
        return round(ImageStat.Stat(ImageChops.difference(a, b)).mean[0], 2)
    except Exception:
        return None


def stage_timings(conn, task_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT stage, duration_ms FROM events WHERE task_id=? AND type='stage_done'", (task_id,)
    ).fetchall()
    return {r["stage"]: int(r["duration_ms"] or 0) for r in rows}


def run_one(conn, settings: Settings, item: dict, workdir: Path) -> dict:
    q = Queue(conn, settings)
    task_id = q.enqueue(item["topic"], item.get("partition"))
    assert q.lease("eval-runner") is not None

    started = time.time()
    try:
        final = run_task(conn, task_id, "eval-runner", settings)
    except Exception as exc:  # noqa: BLE001
        final = f"CRASH:{exc!r}"
    wall = round(time.time() - started, 1)

    row = conn.execute("SELECT status, error, budget_spent_cents FROM tasks WHERE id=?", (task_id,)).fetchone()
    video = artifact(conn, settings, task_id, Stage.RENDER)
    report = artifact(conn, settings, task_id, Stage.QC)
    audio = artifact(conn, settings, task_id, Stage.VOICE)

    duration = float(video.get("duration_sec") or 0)
    motion = None
    if video.get("path"):
        motion = motion_score(Path(settings.artifacts_dir) / video["path"], duration, workdir)

    return {
        "id": item["id"],
        "partition": item.get("partition"),
        "topic": item["topic"],
        "status": row["status"] if row else final,
        "wall_sec": wall,
        "duration_sec": duration,
        "duration_ok": 45.0 <= duration <= 90.0,
        "motion_score": motion,
        "qc_score": report.get("score"),
        "qc_decided": report.get("decided"),
        "audio_degraded": audio.get("degraded_count"),
        "black": video.get("black_segments"),
        "silence": video.get("silence_segments"),
        "cost_cents": int(row["budget_spent_cents"] or 0) if row else 0,
        "error": (row["error"] if row else "") or "",
        "stages_ms": stage_timings(conn, task_id),
    }


def summarize(results: list[dict]) -> dict:
    n = len(results)
    delivered = [r for r in results if r["status"] in {"DONE", "PARTIAL"}]
    strict = [r for r in results if r["status"] == "DONE"]
    durations = [r["duration_sec"] for r in delivered if r["duration_sec"]]
    walls = [r["wall_sec"] for r in results]
    motions = [r["motion_score"] for r in delivered if r["motion_score"] is not None]
    scores = [r["qc_score"] for r in delivered if r["qc_score"] is not None]
    return {
        "n": n,
        "delivered": len(delivered),
        "success_rate": round(100 * len(delivered) / n, 1),
        "strict_rate": round(100 * len(strict) / n, 1),
        "duration_ok_rate": round(100 * sum(1 for r in delivered if r["duration_ok"]) / max(1, len(delivered)), 1),
        "motion_ok_rate": round(100 * sum(1 for m in motions if m >= 1.0) / max(1, len(motions)), 1),
        "duration_median": round(statistics.median(durations), 1) if durations else 0,
        "duration_min": min(durations) if durations else 0,
        "duration_max": max(durations) if durations else 0,
        "wall_median": round(statistics.median(walls), 1) if walls else 0,
        "wall_p95": round(sorted(walls)[int(len(walls) * 0.95) - 1], 1) if walls else 0,
        "motion_median": round(statistics.median(motions), 2) if motions else 0,
        "qc_mean": round(statistics.mean(scores), 1) if scores else 0,
        "cost_total_cents": sum(r["cost_cents"] for r in results),
    }


def write_report(results: list[dict], summary: dict, config: dict, out: Path) -> None:
    lines = [
        "# 评测报告",
        "",
        f"- 时间：{config['time']}",
        f"- 模型：{config['model']}（Mock 时成本为 0）",
        f"- 配音：{config['tts']}　渲染：{config['render']}",
        f"- 样本：{summary['n']} 条（5 个分区）",
        "",
        "## 总览",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 可交付成功率（DONE+PARTIAL） | **{summary['success_rate']}%**（{summary['delivered']}/{summary['n']}） |",
        f"| 严格成功率（仅 DONE） | {summary['strict_rate']}% |",
        f"| 时长合规率（45–90s） | {summary['duration_ok_rate']}% |",
        f"| 画面有运动率（motion ≥ 1.0） | {summary['motion_ok_rate']}% |",
        f"| 时长 中位数 / 区间 | {summary['duration_median']}s / {summary['duration_min']}–{summary['duration_max']}s |",
        f"| 单条耗时 中位数 / P95 | {summary['wall_median']}s / {summary['wall_p95']}s |",
        f"| motion_score 中位数 | {summary['motion_median']} |",
        f"| QC 均分 | {summary['qc_mean']} |",
        f"| 总成本 | {summary['cost_total_cents']} 分 |",
        "",
        "## 逐条结果",
        "",
        "| # | 分区 | 主题 | 状态 | 时长 | motion | QC | 耗时 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['partition']} | {r['topic']} | {r['status']} | "
            f"{r['duration_sec']}s | {r['motion_score']} | {r['qc_score']} | {r['wall_sec']}s |"
        )

    failed = [r for r in results if r["status"] not in {"DONE", "PARTIAL"}]
    lines += ["", "## 失败清单", ""]
    if failed:
        for r in failed:
            lines.append(f"- `{r['id']}` {r['topic']} → {r['status']}：{r['error'][:180]}")
    else:
        lines.append("无。")

    stage_keys = ["RESEARCH", "SCRIPT", "STORYBOARD", "ASSETS", "VOICE", "RENDER", "QC"]
    lines += ["", "## 分阶段耗时（中位数，ms）", "", "| 阶段 | ms |", "|---|---|"]
    for key in stage_keys:
        values = [r["stages_ms"].get(key) for r in results if r["stages_ms"].get(key) is not None]
        if values:
            lines.append(f"| {key} | {int(statistics.median(values))} |")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--offset", type=int, default=0, help="跳过前 N 条（续跑用）")
    ap.add_argument("--out", default="eval/report.md")
    ap.add_argument("--json", default="eval/results.json")
    ap.add_argument("--keep", action="store_true", help="复用已有 eval.db（默认清空）")
    ap.add_argument("--db", default="eval.db")
    args = ap.parse_args()

    settings = draft_settings(
        db_path=args.db,
        artifacts_dir="artifacts_eval_real" if args.db != "eval.db" else "artifacts_eval",
    )
    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            Path(settings.db_path + suffix).unlink(missing_ok=True)

    conn = connect(settings.db_path)
    init_db(conn)
    items = load_golden(ROOT / "eval" / "golden.jsonl", args.limit, args.offset)
    workdir = Path("dev_out/eval"); workdir.mkdir(parents=True, exist_ok=True)

    print(f"[eval] {len(items)} tasks, model={settings.llm_model or 'mock'}, "
          f"render={settings.render_width}x{settings.render_height}@{settings.render_fps}")
    results = []
    for i, item in enumerate(items, 1):
        r = run_one(conn, settings, item, workdir)
        results.append(r)
        print(f"  [{i:02d}/{len(items)}] {r['id']} {r['status']:<7} {r['duration_sec']:>5}s "
              f"motion={r['motion_score']} qc={r['qc_score']} {r['wall_sec']}s")

    summary = summarize(results)
    config = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model": settings.llm_model or "MockLLM",
        "tts": settings.tts_provider,
        "render": f"{settings.render_width}x{settings.render_height}@{settings.render_fps} {settings.render_preset}",
    }
    write_report(results, summary, config, ROOT / args.out)
    (ROOT / args.json).write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    print("\n== summary ==")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
