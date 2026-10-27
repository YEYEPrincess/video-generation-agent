"""ASR：把视频里的中文语音转成带时间戳的句子。

用 faster-whisper（本地部署，不需要 API key），模型文件来自 Hugging Face。
国内直连 HF 经常超时，所以默认走 `hf-mirror.com` 镜像——这一步不写进代码会让人
在第一次运行时卡半小时，属于"能不能跑起来"的关键细节。

为什么不用云 ASR：
- 本机 CPU 跑 small 模型，53 秒音频约 40 秒，成本为零；
- 云 ASR 需要账号、按量计费，而且我们只需要验证链路。
代价是速度慢、模型小、专有名词容易错——这些都会记进评测。
"""

from __future__ import annotations

import os
from pathlib import Path

from app.dub.types import Segment
from app.errors import AppError, ErrCode
from app.settings import Settings

_MODEL_CACHE: dict[str, object] = {}


def _load_model(settings: Settings):
    from faster_whisper import WhisperModel  # 延迟导入：没装 ASR 依赖时不影响其他功能

    name = settings.dub_asr_model
    if name not in _MODEL_CACHE:
        os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
        try:
            _MODEL_CACHE[name] = WhisperModel(name, device="cpu", compute_type="int8")
        except Exception as exc:  # noqa: BLE001
            raise AppError(ErrCode.E_TOOL_UPSTREAM, f"whisper 模型加载失败({name}): {exc}") from exc
    return _MODEL_CACHE[name]


def transcribe(audio: Path, settings: Settings) -> list[Segment]:
    """返回按时间排序的句子列表。"""
    if not audio.exists():
        raise AppError(ErrCode.E_VALIDATION, f"音频不存在: {audio}", retryable=False)

    model = _load_model(settings)
    try:
        raw, info = model.transcribe(  # type: ignore[attr-defined]
            str(audio),
            language=settings.dub_source_lang,
            vad_filter=True,           # 先做语音活动检测，去掉纯静音段，减少空转
            beam_size=5,
        )
    except Exception as exc:  # noqa: BLE001
        raise AppError(ErrCode.E_TOOL_UPSTREAM, f"转录失败: {exc}") from exc

    segments: list[Segment] = []
    for item in raw:
        text = (item.text or "").strip()
        if not text:
            continue
        segments.append(Segment(idx=len(segments), start=round(item.start, 2),
                                end=round(item.end, 2), text=text))
    return segments
