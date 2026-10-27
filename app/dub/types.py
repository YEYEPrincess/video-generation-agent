"""译制流水线的数据结构：每一步的"合同"。

初学者最容易忽略的一点：**流水线里流动的不是字符串，是带时间戳的结构**。
如果字幕丢了 start/end，后面就没法把英文配音放回正确的位置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class Segment:
    """一句台词。start/end 是原视频时间轴上的秒数。"""

    idx: int
    start: float
    end: float
    text: str                     # 原文（中文）
    translated: str = ""          # 译文（目标语言）

    @property
    def slot(self) -> float:
        """这一句在原视频里占的时长——配音必须尽量塞进这个预算。"""
        return max(0.0, self.end - self.start)


@dataclass(slots=True)
class Clip:
    """一段合成出来的配音。"""

    idx: int
    path: Path
    duration: float            # 实际时长（ffprobe 实测，不是估算）
    placed_at: float = 0.0     # 最终放在时间轴的哪个位置
    speed: float = 1.0         # 为了塞进 slot 做了多少倍速
    overflow: float = 0.0      # 还是塞不下时，超出多少秒


@dataclass(slots=True)
class DubResult:
    video: Path
    srt: Path
    audio: Path
    segments: list[Segment] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
