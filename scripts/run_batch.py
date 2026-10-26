"""批量跑真实任务，并把成片复制到便于查看的目录。

    python scripts/run_batch.py --n 5

产物两处都有：
- 原始：`artifacts/<task_id>/RENDER/final.mp4`（含全部中间产物与 trace）
- 便捷：`dev_out/videos/<序号>_<id>_<主题>.mp4`
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.queue import Queue  # noqa: E402
from app.core.runner import run_task  # noqa: E402
from app.core.state_machine import Stage  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.settings import Settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--out", default="dev_out/videos")
    ap.add_argument("--name", default="batch")
    args = ap.parse_args()

    settings = Settings()
    print(f"model={settings.llm_model or 'mock'} img={settings.img_model} "
          f"render={settings.render_width}x{settings.render_height}", flush=True)

    items = [json.loads(line) for line in
             (ROOT / "eval" / "golden.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()][: args.n]

    conn = connect(settings.db_path)
    init_db(conn)
    q = Queue(conn, settings)
    outdir = ROOT / args.out
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    started = time.time()
    for i, item in enumerate(items, 1):
        task_id = q.enqueue(item["topic"], item.get("partition"))
        assert q.lease(args.name) is not None
        t0 = time.time()
        try:
            final = run_task(conn, task_id, args.name, settings)
        except Exception as exc:  # noqa: BLE001
            final = f"CRASH:{exc!r}"
        wall = round(time.time() - t0, 1)

        row = conn.execute(
            "SELECT path FROM artifacts WHERE task_id=? AND stage=? ORDER BY version DESC LIMIT 1",
            (task_id, Stage.RENDER.value),
        ).fetchone()
        meta = Path(settings.artifacts_dir) / row["path"] if row else None
        duration = 0.0
        dst = None
        if meta and meta.exists():
            payload = json.loads(meta.read_text(encoding="utf-8"))
            duration = payload.get("duration_sec", 0)
            # 注意：artifacts 表里存的是 render.json 的路径，
            # 真正的成片是 payload["path"]（即 <task>/RENDER/final.mp4）
            video = Path(settings.artifacts_dir) / payload["path"]
            if video.exists():
                dst = outdir / f"{i:02d}_{item['id']}_{item['topic'][:10]}.mp4"
                shutil.copyfile(video, dst)

        print(f"[{i}/{len(items)}] {item['id']} {final:<7} {duration:>5}s {wall:>6}s -> {dst}", flush=True)
        rows.append({"id": item["id"], "topic": item["topic"], "status": final,
                     "duration": duration, "wall": wall, "task_id": task_id,
                     "src": str(video) if dst else None,
                     "copy": str(dst) if dst else None})

    total = round(time.time() - started, 1)
    print(f"\n完成 {len(rows)} 条，总耗时 {total}s", flush=True)
    print(f"成片副本目录: {outdir.resolve()}", flush=True)
    print(f"原始产物目录: {(Path(settings.artifacts_dir)).resolve()}", flush=True)
    (outdir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
