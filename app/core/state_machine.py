"""状态机：唯一的真相来源（ARCHITECTURE.md §2）。

这里只放"定义"和"纯函数判断"，不放 IO。
"""

from __future__ import annotations

from enum import Enum


class Stage(str, Enum):
    RESEARCH = "RESEARCH"
    SCRIPT = "SCRIPT"
    STORYBOARD = "STORYBOARD"
    ASSETS = "ASSETS"
    VOICE = "VOICE"
    RENDER = "RENDER"
    QC = "QC"


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.RESEARCH,
    Stage.SCRIPT,
    Stage.STORYBOARD,
    Stage.ASSETS,
    Stage.VOICE,
    Stage.RENDER,
    Stage.QC,
)
STAGE_INDEX = {s: i for i, s in enumerate(STAGE_ORDER)}


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


TERMINAL_TASK_STATUSES = {
    TaskStatus.DONE,
    TaskStatus.PARTIAL,
    TaskStatus.FAILED,
    TaskStatus.NEEDS_HUMAN,
}

#: 阶段超时（秒）——PROJECT-PLAN.md 3.1
STAGE_TIMEOUT_SEC: dict[Stage, int] = {
    Stage.RESEARCH: 120,
    Stage.SCRIPT: 120,
    Stage.STORYBOARD: 90,
    Stage.ASSETS: 300,
    Stage.VOICE: 180,
    Stage.RENDER: 240,
    Stage.QC: 120,
}

#: 阶段预算上限（分）——PROJECT-PLAN.md 3.1
STAGE_BUDGET_CENTS: dict[Stage, int] = {
    Stage.RESEARCH: 5,
    Stage.SCRIPT: 8,
    Stage.STORYBOARD: 5,
    Stage.ASSETS: 45,
    Stage.VOICE: 5,
    Stage.RENDER: 0,
    Stage.QC: 8,
}

#: 改了 prompt / 模型 / 参数就把对应数字 +1，幂等键会失效 → 全量重算
STAGE_CONFIG_VERSION: dict[Stage, int] = {s: 1 for s in STAGE_ORDER}

#: 哪些阶段由模型决策（其余是确定性 workflow）
AGENT_STAGES = {Stage.RESEARCH, Stage.ASSETS, Stage.QC}

#: 每个阶段**真正依赖**的上游产物。
#:
#: 注意 VOICE 依赖 STORYBOARD（要旁白原文），不是"上一个阶段" ASSETS。
#: 这决定了 QC 回退重做素材时，VOICE 的幂等键不变 → 自动复用，不重跑配音。
STAGE_INPUTS: dict[Stage, tuple[Stage, ...]] = {
    Stage.RESEARCH: (),
    Stage.SCRIPT: (Stage.RESEARCH,),
    Stage.STORYBOARD: (Stage.SCRIPT,),
    Stage.ASSETS: (Stage.STORYBOARD,),
    Stage.VOICE: (Stage.STORYBOARD,),
    Stage.RENDER: (Stage.ASSETS, Stage.VOICE),
    Stage.QC: (Stage.RENDER, Stage.ASSETS, Stage.VOICE, Stage.STORYBOARD),
}

#: 回退时允许被重置的阶段（VOICE 故意不在列表里：它应该被幂等复用）
ROLLBACK_RESET_STAGES = (Stage.ASSETS, Stage.RENDER, Stage.QC)


def next_stage(stage: Stage) -> Stage | None:
    i = STAGE_INDEX[stage]
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


def prev_stage(stage: Stage) -> Stage | None:
    i = STAGE_INDEX[stage]
    return STAGE_ORDER[i - 1] if i > 0 else None


def is_terminal(status: str) -> bool:
    return TaskStatus(status) in TERMINAL_TASK_STATUSES
