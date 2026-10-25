"""外部能力工具：图像生成、语音合成、媒体探测。

约定：工具**不抛业务异常给上层猜**，要么返回结果，要么抛 `AppError`；
降级（占位图 / 静音）由工具自己完成，并在结果里用 `degraded` 标记。
"""

from app.agent.tools.media import probe, run_ffmpeg
from app.agent.tools.tts import TtsResult, synthesize

__all__ = ["probe", "run_ffmpeg", "synthesize", "TtsResult"]
