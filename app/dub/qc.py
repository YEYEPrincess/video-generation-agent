"""译制质检：把"看起来还行"变成可核对的数字。

针对译制场景，真正会翻车的是这四件事：
1. 成片时长和原片差太多（说明有片段被吞/被拖）；
2. 字幕条数和配音条数对不上；
3. 配音被加速得太狠（1.6 倍以上人耳能听出来）；
4. 时间轴漂移累积（后半段越拖越晚）。
"""

from __future__ import annotations

from app.dub.types import Clip, Segment


def check(segments: list[Segment], clips: list[Clip], src_duration: float, out_duration: float,
          srt_count: int) -> dict:
    total_speech = round(sum(c.duration for c in clips), 2)
    max_speed = max((c.speed for c in clips), default=1.0)
    max_overflow = max((c.overflow for c in clips), default=0.0)
    drift = round(max(0.0, (clips[-1].placed_at + clips[-1].duration) - segments[-1].end), 2) if clips else 0.0

    checks = {
        "duration_match": abs(out_duration - src_duration) <= 1.5,
        "segment_count": len(segments) == len(clips) == srt_count,
        "speed_reasonable": max_speed <= 1.45,
        "no_major_overflow": max_overflow <= 1.0,
        "coverage": total_speech >= 0.35 * max(src_duration, 1e-6),
    }
    return {
        "segments": len(segments),
        "srt_lines": srt_count,
        "src_duration": round(src_duration, 2),
        "out_duration": round(out_duration, 2),
        "speech_seconds": total_speech,
        "max_speed": round(max_speed, 2),
        "max_overflow": round(max_overflow, 2),
        "tail_drift": drift,
        "checks": checks,
        "passed": all(checks.values()),
    }
