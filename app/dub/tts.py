"""逐句配音：目标语言 TTS。

直接复用生成流水线里的 `synthesize()`：它已经处理了
"edge-tts 失败重试 + 尾部静音剪除 + 实测时长"这三件事，没必要重写。
唯一新增的是 `voice` 参数——生成流水线念中文，译制要念英文。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.tools.tts import synthesize
from app.dub.types import Clip, Segment
from app.errors import AppError, ErrCode
from app.settings import Settings


def synthesize_segments(segments: list[Segment], workdir: Path, settings: Settings) -> list[Clip]:
    clips: list[Clip] = []
    for seg in segments:
        text = seg.translated or seg.text
        if not text.strip():
            continue
        try:
            result = synthesize(text, workdir, seg.idx, settings, voice=settings.dub_voice)
        except AppError as exc:
            raise AppError(ErrCode.E_TOOL_UPSTREAM, f"第 {seg.idx} 句配音失败: {exc.message}") from exc
        clips.append(Clip(idx=seg.idx, path=Path(result.path), duration=result.duration_sec))
    if not clips:
        raise AppError(ErrCode.E_VALIDATION, "没有任何一句成功配音", retryable=False)
    return clips
