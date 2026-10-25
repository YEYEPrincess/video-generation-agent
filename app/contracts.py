"""阶段产物契约（纯数据，无 IO、无业务逻辑）。

约定：
- 每个阶段一个顶层模型，带 `version` 字段便于演进；
- 校验规则**不写在这里**，放在 `app/stages/validators.py`，
  因为评测时需要单独替换阈值而不改动数据结构。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- RESEARCH
class Ref(BaseModel):
    title: str = Field(max_length=60)
    ref: str = Field(max_length=120, description="bvid 或 url")
    why: str = Field(max_length=40)


class ResearchBrief(BaseModel):
    version: Literal[1] = 1
    partition: str = Field(default="知识", max_length=20)
    angle: str = Field(min_length=10, max_length=40)
    hooks: list[str] = Field(min_length=3, max_length=3)
    refs: list[Ref] = Field(min_length=1, max_length=5)
    keywords: list[str] = Field(min_length=3, max_length=6)


# ---------------------------------------------------------------- SCRIPT
class Segment(BaseModel):
    idx: int = Field(ge=0)
    text: str = Field(min_length=4, max_length=120)
    est_sec: float = Field(gt=0, le=30)


class Script(BaseModel):
    version: Literal[1] = 1
    title: str = Field(max_length=30)
    hook: str = Field(max_length=40)
    segments: list[Segment] = Field(min_length=3, max_length=10)
    total_sec: float
    tts_rate: float = 4.2


# ---------------------------------------------------------------- STORYBOARD
class Shot(BaseModel):
    idx: int = Field(ge=0)
    segment_idx: int = Field(ge=0)
    narration: str = Field(min_length=4, max_length=120)
    visual_prompt: str = Field(max_length=120)
    motion: Literal["none", "kenburns_in", "kenburns_out", "pan_lr", "i2v"] = "kenburns_in"
    kind_hint: Literal["image", "video"] = "image"
    est_sec: float = Field(gt=0, le=20)


class Storyboard(BaseModel):
    version: Literal[1] = 1
    shots: list[Shot] = Field(min_length=3, max_length=10)


# ---------------------------------------------------------------- ASSETS
class AssetShot(BaseModel):
    idx: int
    kind: Literal["image", "video"]
    path: str
    width: int = 0
    height: int = 0
    sim_score: float | None = None
    degraded: bool = False
    provider: str = "unknown"
    cost_cents: int = 0
    attempts: int = 1


class Assets(BaseModel):
    version: Literal[1] = 1
    shots: list[AssetShot]
    total_cost_cents: int = 0
    degraded_count: int = 0


# ---------------------------------------------------------------- VOICE
class AudioShot(BaseModel):
    idx: int
    path: str
    dur_sec: float = Field(gt=0)


class Audio(BaseModel):
    version: Literal[1] = 1
    per_shot: list[AudioShot]
    srt_path: str = ""
    total_dur: float = 0.0
    voice: str = "zh-CN-XiaoxiaoNeural"
    alignment: Literal["asr", "estimate"] = "estimate"


# ---------------------------------------------------------------- RENDER
class Video(BaseModel):
    version: Literal[1] = 1
    path: str
    duration_sec: float = 0.0
    size_bytes: int = 0
    width: int = 0
    height: int = 0
    fps: int = 24
    has_audio: bool = False
    black_segments: int = 0
    silence_segments: int = 0
    peak_db: float = -99.0


# ---------------------------------------------------------------- QC
class Check(BaseModel):
    name: str
    passed: bool
    detail: str = ""
    severity: Literal["block", "warn"] = "block"


class QCReport(BaseModel):
    version: Literal[1] = 1
    checks: list[Check] = []
    retry_shots: list[int] = []
    score: int = 0
    degraded_reason: str | None = None
    decided: Literal["DONE", "PARTIAL", "RETRY", "NEEDS_HUMAN"] = "DONE"


STAGE_CONTRACTS: dict[str, type[BaseModel]] = {
    "RESEARCH": ResearchBrief,
    "SCRIPT": Script,
    "STORYBOARD": Storyboard,
    "ASSETS": Assets,
    "VOICE": Audio,
    "RENDER": Video,
    "QC": QCReport,
}
