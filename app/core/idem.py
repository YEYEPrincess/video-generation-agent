"""幂等键：让「断点续跑」和「精准重做」共用同一套机制。

idem_key = sha1(task_id | stage | inputs_hash | STAGE_CONFIG_VERSION[stage])[:16]

- 上游产物没变 → 键不变 → 直接复用（StageStatus.SKIPPED）
- 上游变了（例如 QC 换掉第 3 镜素材）→ 键变 → 只重算受影响阶段
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.state_machine import STAGE_CONFIG_VERSION, Stage

#: 参与 hash 时要剔除的易变字段（否则每次生成都不同，幂等全废）
VOLATILE_KEYS = {"created_at", "updated_at", "ts", "cost_cents", "attempts", "provider"}


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _strip_volatile(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def inputs_hash(upstream: Any) -> str:
    """上游产物指纹。空上游（RESEARCH）用固定串，保证可复现。"""
    if not upstream:
        return short_hash("__root__")
    return short_hash(canonical_json(_strip_volatile(upstream)))


def idem_key(task_id: str, stage: Stage, upstream: Any) -> str:
    raw = f"{task_id}|{stage.value}|{inputs_hash(upstream)}|{STAGE_CONFIG_VERSION[stage]}"
    return short_hash(raw)
