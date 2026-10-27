"""译制的音频工具：抽音轨、倍速拟合、把多段配音铺回时间轴。

时间轴是译制里最难的一步，原因很简单：
**同一句话，英文通常比中文长 30%–50%**。如果每句都慢悠悠念完，后面就会越拖越晚，
最后画面和声音完全对不上。所以只有两条路：
1. 把每句加速（`atempo`），塞回原来的时间槽；
2. 塞不进去就顺延（会有漂移），并在报告里记下来。

我们两条都用：先尽量加速（上限 `dub_max_speedup`），还不够就顺延，漂移量进指标。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.agent.tools.media import probe
from app.agent.tools.render import _run
from app.dub.types import Clip, Segment
from app.errors import AppError, ErrCode
from app.settings import Settings

SAMPLE_RATE = 24000


def extract_audio(video: Path, out_wav: Path, timeout: int = 300) -> Path:
    """抽成 16k 单声道 wav：whisper 只吃这个规格，且体积小。"""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video.resolve()),
         "-vn", "-ac", "1", "-ar", "16000", str(out_wav.resolve())],
        cwd=out_wav.parent, timeout=timeout,
    )
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"抽音轨失败: {proc.stderr.strip()[-200:]}")
    return out_wav


def fit_clip(clip: Clip, slot: float, settings: Settings, workdir: Path) -> Clip:
    """把一段配音压进 slot 秒。压不下就返回原样，由调用方决定是否顺延。"""
    if slot <= 0.05 or clip.duration <= slot * 1.02:
        return clip

    factor = min(settings.dub_max_speedup, clip.duration / slot)
    if factor <= 1.02:
        return clip

    out = clip.path.with_name(clip.path.stem + f"_x{factor:.2f}" + clip.path.suffix)
    proc = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", clip.path.name,
         "-filter:a", f"atempo={factor:.3f}", "-c:a", "libmp3lame", "-q:a", "4", out.name],
        cwd=workdir, timeout=settings.ffmpeg_timeout_sec,
    )
    if proc.returncode != 0:
        return clip  # 加速失败就保持原速，宁可漂移也不要没声音

    duration = round(probe(out)["duration"], 2)
    return replace(clip, path=out, duration=duration, speed=round(factor, 2))


def place_clips(segments: list[Segment], clips: list[Clip]) -> tuple[list[Clip], float]:
    """决定每段配音最终放在哪里，返回 (clips, 最大漂移秒数)。"""
    cursor = 0.0
    drift_max = 0.0
    placed: list[Clip] = [replace(c, placed_at=0.0) for c in clips]

    for i, (seg, clip) in enumerate(zip(segments, placed)):
        start = max(seg.start, cursor)          # 不早于原位置，也不和上一句重叠
        drift = max(0.0, start - seg.start)
        drift_max = max(drift_max, drift)
        placed[i] = replace(clip, placed_at=round(start, 3),
                            overflow=round(max(0.0, start + clip.duration - seg.end), 2))
        cursor = start + clip.duration + 0.05   # 留 50ms 换气
    return placed, round(drift_max, 2)


def build_track(clips: list[Clip], total_seconds: float, out_path: Path, settings: Settings) -> Path:
    """用一条静音轨做底，把每段配音 adelay 到指定位置再混音。

    为什么不用 concat：每段之间可能有间隔（原视频里本来就有的停顿），
    concat 会把间隔吃掉，时间轴立刻错位。adelay + amix 才能保住"原位"。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = max(total_seconds, max((c.placed_at + c.duration for c in clips), default=0.0)) + 0.4

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-t", f"{total:.3f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=mono"]
    for clip in clips:
        args += ["-i", clip.path.name]

    filters = []
    for i, clip in enumerate(clips, start=1):
        delay_ms = int(round(clip.placed_at * 1000))
        filters.append(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:channel_layouts=mono,"
            f"adelay={delay_ms}|{delay_ms}[a{i}]"
        )
    mix_inputs = "".join(f"[a{i}]" for i in range(1, len(clips) + 1))
    filters.append(f"[0:a]{mix_inputs}amix=inputs={len(clips) + 1}:duration=first:normalize=0[out]")

    args += ["-filter_complex", ";".join(filters), "-map", "[out]",
             "-c:a", "aac", "-b:a", "160k", out_path.name]

    proc = _run(args, cwd=out_path.parent, timeout=settings.ffmpeg_timeout_sec)
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"混音失败: {proc.stderr.strip()[-300:]}")
    return out_path
