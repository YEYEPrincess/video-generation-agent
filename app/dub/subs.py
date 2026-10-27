"""SRT 字幕：用**配音落点**的时间写，而不是原文时间。

这是译制里最容易做错的地方：如果字幕还用原文时间戳，
观众看到的是"英文文字 + 中文语速"，两边对不上。
所以字幕时间必须取 `clip.placed_at / placed_at + duration`。
"""

from __future__ import annotations

from pathlib import Path

from app.dub.types import Clip, Segment


def _ts(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(segments: list[Segment], clips: list[Clip], out_path: Path) -> Path:
    lines: list[str] = []
    for i, (seg, clip) in enumerate(zip(segments, clips), start=1):
        start = clip.placed_at
        end = start + clip.duration
        lines += [str(i), f"{_ts(start)} --> {_ts(end)}", seg.translated or seg.text, ""]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def read_srt_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip().isdigit())
