"""阶段 handler 注册表：Stage -> 可调用对象。"""

from __future__ import annotations

from typing import Callable

from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode

Handler = Callable[[StageContext], StageResult]

HANDLERS: dict[Stage, Handler] = {}


def register(stage: Stage) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[stage] = fn
        return fn

    return deco


def get_handler(stage: Stage) -> Handler:
    try:
        return HANDLERS[stage]
    except KeyError as exc:
        raise AppError(ErrCode.E_INTERNAL, f"no handler registered for stage {stage.value}") from exc
