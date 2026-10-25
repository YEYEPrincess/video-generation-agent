"""媒体工具：ffprobe 探测、ffmpeg 调用。

M3 会重度使用；M2 只用 `probe` 确认音频时长。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.errors import AppError, ErrCode

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def available() -> bool:
    return shutil.which(FFMPEG) is not None and shutil.which(FFPROBE) is not None


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise AppError(ErrCode.E_TOOL_TIMEOUT, f"命令超时 {timeout}s: {cmd[0]}") from exc
    except OSError as exc:
        raise AppError(ErrCode.E_INTERNAL, f"无法执行 {cmd[0]}: {exc}") from exc


def probe(path: str | Path, timeout: int = 20) -> dict[str, Any]:
    """返回 {duration, width, height, has_audio, fps}。文件不可读时抛 E_RENDER。"""
    p = Path(path)
    if not p.exists():
        raise AppError(ErrCode.E_RENDER, f"文件不存在: {p}", retryable=False)

    proc = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(p),
        ],
        timeout,
    )
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"ffprobe 失败: {proc.stderr.strip()[:200]}")

    data = json.loads(proc.stdout or "{}")
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    width = height = 0
    fps = 0.0
    has_audio = False
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio":
            has_audio = True
        elif stream.get("codec_type") == "video":
            width = int(stream.get("width") or width)
            height = int(stream.get("height") or height)
            rate = str(stream.get("r_frame_rate") or "0/1")
            try:
                num, den = rate.split("/")
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                fps = fps or 0.0
    return {"duration": duration, "width": width, "height": height, "has_audio": has_audio, "fps": round(fps, 2)}


def run_ffmpeg(args: list[str], timeout: int = 240) -> None:
    """执行 ffmpeg，非 0 退出统一归一为 E_RENDER。"""
    proc = _run([FFMPEG, "-hide_banner", "-loglevel", "error", *args], timeout)
    if proc.returncode != 0:
        raise AppError(ErrCode.E_RENDER, f"ffmpeg 失败: {proc.stderr.strip()[:300]}")
