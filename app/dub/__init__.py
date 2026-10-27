"""译制（dubbing）流水线：把一条中文视频变成"英文配音 + 英文字幕"的版本。

为什么单独放一个包、而不是塞进 `app/stages/`：
现有七阶段流水线的每一阶段都强依赖 `task/queue/租约`，而译制链路是**另一种任务形状**
（有原视频、有多语言、有时长对齐）。先用独立的纯函数 + CLI 把链路验证清楚，
再决定要不要接进任务队列——这是"先跑通、再工程化"的顺序，反过来会卡在框架里。

链路（每一步的输入输出都是显式 dataclass，便于单测）：

    video → extract_audio → transcribe → translate → synthesize → align → mux → qc
             (ffmpeg)      (whisper)     (LLM)      (edge-tts)  (atempo)  (ffmpeg)
"""

from app.dub.types import Clip, DubResult, Segment

__all__ = ["Clip", "DubResult", "Segment"]
