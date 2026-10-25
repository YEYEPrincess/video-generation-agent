"""SCRIPT 阶段：模型写内容，代码算时间。

分工原则：
- 模型负责"说什么"（表达、节奏、钩子）；
- 代码负责"多长"（字数 → 秒数）、截断、以及 45–90s 的硬约束。

这样即使模型把字数写飞了，也有确定性修复；修不回来就带反馈重试一次。
"""

from __future__ import annotations

from pydantic import ValidationError

from app.agent.llm import extract_json, get_llm, stage_marker
from app.agent.prompts import render
from app.contracts import Check, ResearchBrief, Script, Segment
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages.registry import register
from app.stages.validators import register as register_validator

SYSTEM = stage_marker("SCRIPT") + "\n你是 B站 UP 主兼编导，输出必须是可解析的 JSON。"

MIN_SEC, MAX_SEC = 45.0, 90.0
TAIL_PAD = 0.8  # 片头片尾留白
MAX_SEGMENTS = 10


def estimate_sec(text: str, rate: float) -> float:
    return round(max(1.5, len(text) / rate), 1)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for punct in "。！？，；、":
        idx = cut.rfind(punct)
        if idx >= int(limit * 0.5):
            return cut[: idx + 1]
    return cut


def build_script(raw: dict, rate: float) -> Script:
    segments: list[Segment] = []
    raw_segments = raw.get("segments") or []
    for item in raw_segments:
        text = (item.get("text") if isinstance(item, dict) else str(item)) or ""
        text = str(text).strip()[:120]
        if len(text) < 4:
            continue
        segments.append(Segment(idx=len(segments), text=text, est_sec=estimate_sec(text, rate)))

    if not segments and raw_segments:
        raise AppError(ErrCode.E_VALIDATION, "所有段落都短于 4 个字符，请写出完整句子而不是碎片")
    if not 3 <= len(segments) <= MAX_SEGMENTS:
        raise AppError(
            ErrCode.E_VALIDATION, f"分段数 {len(segments)} 不在 3–{MAX_SEGMENTS}，请合并或拆分段落"
        )

    total = round(sum(s.est_sec for s in segments) + TAIL_PAD, 1)
    title = str(raw.get("title") or "").strip()[:30]
    hook = str(raw.get("hook") or segments[0].text).strip()[:40]
    if not title:
        raise AppError(ErrCode.E_VALIDATION, "缺少标题")
    return Script(title=title, hook=hook, segments=segments, total_sec=total, tts_rate=rate)


def fit_to_max(script: Script) -> Script:
    """超长时按比例截断每段（保留句读），这是确定性修复，不依赖模型。"""
    if script.total_sec <= MAX_SEC:
        return script
    factor = (MAX_SEC - TAIL_PAD - 2.0) / script.total_sec
    segments: list[Segment] = []
    for seg in script.segments:
        limit = max(20, int(len(seg.text) * factor))
        text = _truncate(seg.text, limit)
        segments.append(Segment(idx=len(segments), text=text, est_sec=estimate_sec(text, script.tts_rate)))
    total = round(sum(s.est_sec for s in segments) + TAIL_PAD, 1)
    return Script(title=script.title, hook=script.hook, segments=segments, total_sec=total, tts_rate=script.tts_rate)


@register(Stage.SCRIPT)
def run(ctx: StageContext) -> StageResult:
    try:
        brief = ResearchBrief.model_validate(ctx.upstream or {})
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "上游研究简报非法", detail={"errors": exc.errors()[:3]}) from exc

    llm = get_llm(ctx.settings)
    rate = ctx.settings.tts_rate
    feedback = ""
    last_error: AppError | None = None
    tokens = 0
    cost = 0

    for attempt in (1, 2):
        user = render(
            "script",
            angle=brief.angle,
            hooks="；".join(brief.hooks),
            keywords="、".join(brief.keywords),
        )
        if feedback:
            user += f"\n\n【上一版被程序打回】{feedback}\n请严格按约束重写。"
        res = llm.complete(system=SYSTEM, user=user, max_tokens=1000)
        tokens += res.tokens
        cost += res.cost_cents

        try:
            script = build_script(extract_json(res.text), rate)
            script = fit_to_max(script)
            if script.total_sec > MAX_SEC:
                raise AppError(ErrCode.E_VALIDATION, f"超长且无法截断：{script.total_sec}s > {MAX_SEC}s")
            if script.total_sec < MIN_SEC:
                raise AppError(
                    ErrCode.E_VALIDATION,
                    f"太短：{script.total_sec}s < {MIN_SEC}s，请把总字数写到 210–300 字",
                )
        except AppError as exc:
            last_error = exc
            feedback = exc.message
            continue

        return StageResult(
            data=script.model_dump(),
            tokens=tokens,
            cost_cents=cost,
            meta={"model": res.model, "attempts": attempt, "total_sec": script.total_sec},
        )

    assert last_error is not None
    raise last_error


@register_validator(Stage.SCRIPT)
def validate(data: dict) -> list[Check]:
    total = float(data.get("total_sec") or 0)
    segments = data.get("segments") or []
    internal = round(sum(float(s.get("est_sec") or 0) for s in segments) + TAIL_PAD, 1)
    return [
        Check(name="duration_range", passed=MIN_SEC <= total <= MAX_SEC, detail=f"{total}s ∈ [{MIN_SEC},{MAX_SEC}]"),
        Check(name="segment_count", passed=3 <= len(segments) <= MAX_SEGMENTS, detail=f"{len(segments)} 段"),
        Check(
            name="duration_consistency",
            passed=abs(internal - total) <= 0.2,
            detail=f"分段合计 {internal}s vs 总时长 {total}s",
        ),
        Check(
            name="no_filler_openers",
            passed=not any(str(s.get("text", "")).startswith(("大家好", "首先", "今天我们来")) for s in segments),
            detail="开头不能是寒暄/套话",
        ),
    ]
