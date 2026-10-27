"""译制 CLI：

    python scripts/dub.py --video "dev_out/videos/01_xxx.mp4"
    python scripts/dub.py --video in.mp4 --target en --voice en-US-GuyNeural
    python scripts/dub.py --video in.mp4 --model medium --no-keep-work

产物在 `artifacts_dub/<时间戳>_<视频名>/`：
    final.mp4 / subs.en.srt / dubbed.m4a / asr.json / translation.json / clips.json / report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.dub.pipeline import run  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.settings import Settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--target", default="en")
    ap.add_argument("--voice", default="")
    ap.add_argument("--model", default="", help="whisper 模型：tiny/small/medium")
    ap.add_argument("--glossary", default="", help="术语表 JSON 文件：{\"中文\": \"English\"}")
    ap.add_argument("--cover-y", type=int, default=-1, help="手动指定原字幕覆盖带的上沿（像素）")
    ap.add_argument("--cover-h", type=int, default=-1, help="手动指定覆盖带高度（像素）")
    ap.add_argument("--no-keep-work", action="store_true", help="完成后删掉中间文件")
    args = ap.parse_args()

    settings = Settings()
    updates: dict[str, object] = {"dub_target_lang": args.target}
    if args.voice:
        updates["dub_voice"] = args.voice
    if args.model:
        updates["dub_asr_model"] = args.model
    settings = settings.model_copy(update=updates)

    glossary = None
    if args.glossary:
        glossary = json.loads(Path(args.glossary).read_text(encoding="utf-8"))

    print(f"源视频: {args.video}")
    print(f"目标语言: {settings.dub_target_lang} | 音色: {settings.dub_voice} | ASR: {settings.dub_asr_model}")

    try:
        override = (args.cover_y, args.cover_h) if args.cover_y >= 0 and args.cover_h > 0 else None
        result = run(Path(args.video), settings, glossary=glossary,
                     keep_work=not args.no_keep_work, cover_override=override)
    except AppError as exc:
        print(f"\n[失败] {exc.code.value}: {exc.message}")
        return 1

    m = result.metrics
    print("\n=== 完成 ===")
    print(f"成片: {result.video}")
    print(f"字幕: {result.srt}")
    print(f"时长: {m['src_duration']}s → {m['out_duration']}s")
    print(f"句子: {m['segments']} | 最大加速 {m['max_speed']}x | 尾部漂移 {m['tail_drift']}s")
    print(f"质检: {'通过' if m['passed'] else '未通过'} {m['checks']}")
    return 0 if m["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
