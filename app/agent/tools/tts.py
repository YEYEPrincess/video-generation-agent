"""语音合成：edge-tts 为主，静音 WAV 兜底。

静音兜底不是"糊弄"——它保证 RENDER 永远有音轨，让整条链路不被一个
TTS 服务拖死；结果里用 `provider` 与 `degraded` 如实标注。
"""

from __future__ import annotations

import asyncio
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from app.agent.tools.media import probe
from app.errors import AppError, ErrCode
from app.settings import Settings

SILENT_RATE = 24000


@dataclass(slots=True)
class TtsResult:
    path: str
    duration_sec: float
    provider: str
    degraded: bool = False


def _silent(text: str, out_path: Path, rate: float) -> TtsResult:
    duration = round(max(1.5, len(text) / rate), 2)
    frames = int(SILENT_RATE * duration)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SILENT_RATE)
        wav.writeframes(b"\x00\x00" * frames)
    return TtsResult(path=str(out_path), duration_sec=duration, provider="silent", degraded=True)


async def _edge_save(text: str, out_path: Path, voice: str, timeout: float) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice)
    await asyncio.wait_for(communicate.save(str(out_path)), timeout=timeout)


def _trim_tail(path: Path, timeout: float) -> Path:
    """剪掉 edge-tts 自带的尾部空白（每段约 0.8s），再补 0.25s 换气。

    不剪的话，6 段拼起来就是 6 个明显的"静音段"，silencedetect 一定报警，
    听感上也会像卡带。
    """
    tmp = path.with_name(path.stem + "_trim" + path.suffix)
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
            "-af",
            "areverse,silenceremove=start_periods=1:start_duration=0.30:start_threshold=-45dB,"
            "areverse,apad=pad_dur=0.25",
            "-c:a", "libmp3lame", "-q:a", "4", str(tmp),
        ],
        capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        return path
    try:
        if probe(tmp)["duration"] < 0.5:
            tmp.unlink(missing_ok=True)
            return path
    except AppError:
        tmp.unlink(missing_ok=True)
        return path
    tmp.replace(path)
    return path


def synthesize(text: str, out_dir: Path, idx: int, settings: Settings, voice: str | None = None) -> TtsResult:
    """按镜生成配音。返回的 duration 是**实测**时长，不是估算。

    edge-tts 第一次调用偶尔握手失败（尤其冷启动），所以重试 1 次再降级——
    不重试的话，第一条片子必然有一镜是哑的。
    """
    out_dir.mkdir(parents=True, exist_ok=True)  # edge_tts.save 不会自动建目录

    if settings.tts_provider in {"edge", "auto"}:
        mp3 = out_dir / f"shot_{idx:02d}.mp3"
        for attempt in range(2):
            try:
                asyncio.run(_edge_save(text, mp3, voice or settings.tts_voice, settings.tts_timeout_sec))
                _trim_tail(mp3, settings.tts_timeout_sec)
                info = probe(mp3)
                if info["duration"] <= 0:
                    raise AppError(ErrCode.E_TOOL_UPSTREAM, "edge-tts 产出时长为 0")
                return TtsResult(path=str(mp3), duration_sec=round(info["duration"], 2), provider="edge")
            except Exception:
                if attempt == 0:
                    time.sleep(0.6)
                    continue
                break

    wav = out_dir / f"shot_{idx:02d}.wav"
    return _silent(text, wav, settings.tts_rate)
