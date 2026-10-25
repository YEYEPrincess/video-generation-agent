"""视频合成：图片 → 片段 → 拼接 → 混音 → 烧字幕 → 自检。

时间轴原则：**以音频为准**。每段画面时长 = 对应配音的实测时长，
音频一分不裁；画面不够就定格补，多了就截。这样字幕（由音频累加而来）
永远和画面同步。

所有 ffmpeg 命令都用「工作目录 + 相对文件名」：
Windows 上中文路径 + 空格 + filter 里的冒号会把转义搞成玄学，
切到 work 目录后一律用 ASCII 文件名，问题直接消失。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.errors import AppError, ErrCode


@dataclass(slots=True)
class RenderResult:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    size_bytes: int
    has_audio: bool
    black_segments: int
    silence_segments: int
    peak_db: float


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError(ErrCode.E_TOOL_TIMEOUT, f"ffmpeg 超时 {timeout}s") from exc


def _check(proc: subprocess.CompletedProcess[str], what: str) -> None:
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"{what} 失败: {proc.stderr.strip()[-400:]}")


def build_clip(
    image: Path,
    out: Path,
    duration: float,
    *,
    motion: str,
    width: int,
    height: int,
    fps: int,
    preset: str,
    cwd: Path,
    timeout: int,
) -> None:
    base = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"

    if motion == "none":
        vf = f"{base},format=yuv420p"
        source_args = ["-loop", "1", "-framerate", str(fps), "-i", str(image)]
    else:
        # ⚠️ 关键：zoompan 的 d 必须等于**总帧数**，并且只喂一张图。
        # 用 `-loop 1` 喂多帧 + `d=1` 会让 zoom 每帧重置 —— 效果等于没动。
        frames = max(1, int(round(duration * fps)))
        if motion == "kenburns_out":
            z = "max(1.12-0.0006*on,1.0)"
            x = "iw/2-(iw/zoom/2)"
        elif motion == "pan_lr":
            z = "1.10"
            x = f"(iw-iw/zoom)*on/{frames}"
        else:  # kenburns_in / i2v 静帧
            z = "min(zoom+0.0010,1.15)"
            x = "iw/2-(iw/zoom/2)"
        vf = (
            f"{base},zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={width}x{height}:fps={fps},format=yuv420p"
        )
        source_args = ["-i", str(image)]

    proc = _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            *source_args,
            "-t", f"{duration:.3f}",
            "-vf", vf, "-r", str(fps),
            "-c:v", "libx264", "-preset", preset, "-crf", "23",
            "-pix_fmt", "yuv420p", "-tune", "stillimage",
            out.name,
        ],
        cwd=cwd, timeout=timeout,
    )
    _check(proc, "生成镜头片段")


def build_clip_from_video(
    video: Path,
    out: Path,
    duration: float,
    *,
    width: int,
    height: int,
    fps: int,
    preset: str,
    cwd: Path,
    timeout: int,
) -> None:
    """把一段（可能太短的）视频铺满目标时长：循环 + 裁切到 16:9。"""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
    )
    proc = _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(video.resolve()),
            "-t", f"{duration:.3f}", "-vf", vf,
            "-c:v", "libx264", "-preset", preset, "-crf", "23",
            "-pix_fmt", "yuv420p", out.name,
        ],
        cwd=cwd, timeout=timeout,
    )
    _check(proc, "生成镜头片段（视频素材）")


def concat_video(clips: list[Path], out_name: str, cwd: Path, timeout: int) -> None:
    listing = cwd / "concat.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    proc = _run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", "concat.txt", "-c", "copy", out_name],
        cwd=cwd, timeout=timeout,
    )
    _check(proc, "拼接画面")


def concat_audio(inputs: list[Path], out_name: str, cwd: Path, timeout: int, pad_to: float | None = None) -> None:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for item in inputs:
        args += ["-i", item.name]
    streams = "".join(f"[{i}:a]" for i in range(len(inputs)))
    chain = f"{streams}concat=n={len(inputs)}:v=0:a=1"
    if pad_to:
        # 用静音把音轨补到目标时长，保证成片不会因为音频短而低于下限
        chain += f",apad=whole_dur={pad_to:.3f}"
    args += [
        "-filter_complex", f"{chain}[a]",
        "-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", out_name,
    ]
    proc = _run(args, cwd=cwd, timeout=timeout)
    _check(proc, "拼接音轨")


def mux_with_subtitles(
    video_name: str,
    audio_name: str,
    srt_name: str,
    out_name: str,
    *,
    width: int,
    height: int,
    preset: str,
    cwd: Path,
    timeout: int,
    duration: float | None = None,
) -> None:
    style = (
        "FontName=Microsoft YaHei,FontSize=20,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=3,BackColour=&H80000000,"
        "Outline=1,Shadow=0,MarginV=42,MarginL=60,MarginR=60"
    )
    vf = f"subtitles={srt_name}:force_style='{style}'"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", video_name, "-i", audio_name,
            "-vf", vf, "-c:v", "libx264", "-preset", preset, "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    else:
        args += ["-shortest"]
    args.append(out_name)
    proc = _run(args, cwd=cwd, timeout=timeout)
    _check(proc, "混流 + 烧字幕")


def inspect(path: Path, cwd: Path, timeout: int, duration: float | None = None) -> dict:
    """黑帧 / 静音 / 峰值。

    静音分两类：
    - **片中静音** = 缺陷（卡带、掉帧、空镜）；
    - **片尾静音** = 设计（最后一镜定格 + 音轨补静音凑够 45s）。
    所以 `silence_segments` 只统计片中，片尾单独记在 `tail_silence_sec`。
    """
    video = _run(
        ["ffmpeg", "-hide_banner", "-i", path.name, "-an",
         "-vf", "blackdetect=d=0.5:pix_th=0.10", "-f", "null", "-"],
        cwd=cwd, timeout=timeout,
    )
    audio = _run(
        ["ffmpeg", "-hide_banner", "-i", path.name, "-vn",
         "-af", "silencedetect=noise=-40dB:d=0.5,volumedetect", "-f", "null", "-"],
        cwd=cwd, timeout=timeout,
    )

    text = f"{video.stderr}\n{audio.stderr}"
    peaks = [float(m) for m in re.findall(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", text)]

    interior = 0
    tail = 0.0
    for match in re.finditer(
        r"silence_start:\s*(-?[\d.]+)(?:.*?silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+))?",
        audio.stderr,
        flags=re.S,
    ):
        start, end, length = match.groups()
        end_value = float(end) if end else None
        span = float(length) if length else 0.0
        if end_value is None or (duration and end_value >= duration - 0.35):
            tail += span or (duration - float(start) if duration else 0.0)
        else:
            interior += 1

    return {
        "black_segments": len(re.findall(r"black_start:", video.stderr)),
        "silence_segments": interior,
        "tail_silence_sec": round(tail, 2),
        "peak_db": max(peaks) if peaks else -99.0,
    }


def probe_basic(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
         "-show_entries", "stream=codec_type,width,height,r_frame_rate",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"ffprobe 失败: {proc.stderr.strip()[:200]}")

    info = {"duration": 0.0, "size": 0, "width": 0, "height": 0, "fps": 0.0, "has_audio": False}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key == "duration":
            info["duration"] = float(value or 0)
        elif key == "size":
            info["size"] = int(value or 0)
        elif key == "codec_type" and value == "audio":
            info["has_audio"] = True
        elif key == "width":
            info["width"] = int(value or 0)
        elif key == "height":
            info["height"] = int(value or 0)
        elif key == "r_frame_rate" and "/" in value:
            num, _, den = value.partition("/")
            try:
                numerator, denominator = float(num), float(den)
                # 音轨也有 r_frame_rate=0/0，别被它覆盖成 0
                if numerator > 0 and denominator > 0:
                    info["fps"] = round(numerator / denominator, 2)
            except ValueError:
                pass
    return info


def prepare_workdir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path
