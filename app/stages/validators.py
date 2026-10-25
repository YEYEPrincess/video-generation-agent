"""阶段校验器：数据（contracts）与规则（validators）分离。

- 返回 `Check` 列表；任何 `severity="block"` 且 `passed=False` 都判该阶段失败；
- 评测时可以单独替换阈值，不需要改数据结构。
"""

from __future__ import annotations

from typing import Callable

from app.contracts import Check
from app.core.state_machine import Stage

Validator = Callable[[dict], list[Check]]

VALIDATORS: dict[Stage, Validator] = {}


def register(stage: Stage) -> Callable[[Validator], Validator]:
    def deco(fn: Validator) -> Validator:
        VALIDATORS[stage] = fn
        return fn

    return deco


def validate(stage: Stage, data: dict) -> list[Check]:
    fn = VALIDATORS.get(stage)
    return fn(data) if fn else []
