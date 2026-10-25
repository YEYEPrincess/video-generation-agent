"""M0 假阶段：只验证"壳"能跑通，不产出真实内容。

每个假阶段 sleep 一小段时间，让 RUNNING 状态和进度条肉眼可见。
M1 起，真实 handler 在各自的 `app/stages/<name>.py` 里覆盖注册。
"""

from __future__ import annotations

import time

from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.stages.registry import register


def _fake(stage: Stage):
    @register(stage)
    def _handler(ctx: StageContext) -> StageResult:
        time.sleep(ctx.settings.fake_stage_sleep)
        return StageResult(
            data={
                "_fake": True,
                "stage": stage.value,
                "task_id": ctx.task_id,
                "upstream_keys": sorted(ctx.upstream.keys()),
                "note": "M0 placeholder — replaced by real handler in M1+",
            },
            cost_cents=0,
            tokens=0,
            validate_contract=False,
        )

    _handler.__name__ = f"fake_{stage.value.lower()}"
    return _handler


for _stage in Stage:
    _fake(_stage)
