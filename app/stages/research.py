"""RESEARCH 阶段：把主题变成"选题简报"。

为什么这一步交给模型：判断"从哪个角度切"是语义决策，写死在代码里就退化成模板。
"""

from __future__ import annotations

from pydantic import ValidationError

from app.agent.llm import extract_json, get_llm, stage_marker
from app.agent.prompts import render
from app.contracts import Check, ResearchBrief
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages.registry import register
from app.stages.validators import register as register_validator

SYSTEM = (
    stage_marker("RESEARCH")
    + "\n你是 B站 知识区的选题策划，输出必须是可解析的 JSON，不要任何解释文字。"
)


@register(Stage.RESEARCH)
def run(ctx: StageContext) -> StageResult:
    llm = get_llm(ctx.settings)
    user = render("research", topic=ctx.topic, partition=ctx.partition or "知识")
    res = llm.complete(system=SYSTEM, user=user, max_tokens=800)

    raw = extract_json(res.text)
    try:
        brief = ResearchBrief.model_validate(raw)
    except ValidationError as exc:
        raise AppError(
            ErrCode.E_VALIDATION,
            "研究简报不符合契约",
            detail={"errors": exc.errors(include_url=False)[:3]},
        ) from exc

    return StageResult(
        data=brief.model_dump(),
        tokens=res.tokens,
        cost_cents=res.cost_cents,
        meta={"model": res.model},
    )


@register_validator(Stage.RESEARCH)
def validate(data: dict) -> list[Check]:
    checks = [
        Check(name="refs_not_empty", passed=len(data.get("refs") or []) >= 1, detail="至少 1 条站内参考"),
        Check(name="hooks_count", passed=len(data.get("hooks") or []) == 3, detail="必须 3 个钩子"),
        Check(
            name="angle_specific",
            passed=len(data.get("angle") or "") >= 10,
            detail="角度要有信息量（≥10 字）",
        ),
    ]
    return checks
