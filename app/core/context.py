"""阶段执行的上下文对象（单独放这里，避免 runner 与 registry 循环 import）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.core.state_machine import Stage
from app.settings import Settings


@dataclass(slots=True)
class StageContext:
    task_id: str
    topic: str
    partition: str | None
    stage: Stage
    upstream: dict[str, Any]
    attempt: int
    settings: Settings
    conn: sqlite3.Connection


@dataclass(slots=True)
class StageResult:
    data: dict[str, Any]
    cost_cents: int = 0
    tokens: int = 0
    #: M0 的假阶段产物不是真契约，显式关掉；真实 handler 必须保持 True
    validate_contract: bool = True
    meta: dict[str, Any] = field(default_factory=dict)
