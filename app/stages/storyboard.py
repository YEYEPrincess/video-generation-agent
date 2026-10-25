"""STORYBOARD 阶段：**纯代码**，不调模型。

理由：把「一段稿子切成几个镜头、每个镜头多长」交给模型，只会引入方差；
切分有确定算法（按句读 → 限长合并 → 数量约束），代码做更快、更稳、可测。

但 `visual_prompt` 目前是模板生成的（主体 + 风格 + 构图），
M6 会评估是否用一次批量模型调用提升语义相关性（见 docs/debt.md）。
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from app.contracts import Check, Script, Shot, Storyboard
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages.registry import register
from app.stages.validators import register as register_validator

MIN_SHOTS, MAX_SHOTS = 3, 10
CHUNK_LIMIT = 52  # 单镜旁白上限，约 12 秒

STYLES = {
    "知识": "扁平插画，深蓝与暖橙撞色，柔和侧光，干净背景",
    "科技": "科技感三维渲染，冷色调，细线光效，暗背景",
    "生活": "生活纪实摄影，自然光，浅景深，真实质感",
    "游戏": "游戏概念原画，高对比，动态构图，粒子特效",
    "影视": "电影感剧照，低饱和，宽画幅，气氛光",
}
COMPOSITIONS = ["近景特写", "中景", "全景"]
MOTIONS = ["kenburns_in", "kenburns_out", "pan_lr"]

_SENT_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def split_sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in _SENT_RE.finditer(text) if m.group(0).strip()]


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """按句读切，超长再按逗号切，最后把过短的合并。"""
    parts: list[str] = []
    for sent in split_sentences(text):
        while len(sent) > limit:
            cut = sent.rfind("，", 0, limit)
            cut = cut + 1 if cut > limit * 0.5 else limit
            parts.append(sent[:cut])
            sent = sent[cut:]
        if sent:
            parts.append(sent)

    merged: list[str] = []
    for part in parts:
        if merged and len(merged[-1]) + len(part) <= limit:
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def _estimate(text: str, rate: float) -> float:
    return round(max(1.5, len(text) / rate), 1)


def _merge_to_max(shots: list[Shot]) -> list[Shot]:
    while len(shots) > MAX_SHOTS:
        best, best_len = 0, 10**9
        for i in range(len(shots) - 1):
            size = len(shots[i].narration) + len(shots[i + 1].narration)
            if size < best_len:
                best, best_len = i, size
        left, right = shots[best], shots[best + 1]
        shots[best] = left.model_copy(
            update={"narration": left.narration + right.narration, "est_sec": left.est_sec + right.est_sec}
        )
        del shots[best + 1]
    return shots


def _split_to_min(shots: list[Shot], rate: float) -> list[Shot]:
    while len(shots) < MIN_SHOTS:
        idx = max(range(len(shots)), key=lambda i: len(shots[i].narration))
        target = shots[idx]
        text = target.narration
        cut = text.rfind("，", 0, len(text) // 2 + 6)
        cut = cut + 1 if cut > 4 else max(4, len(text) // 2)
        head, tail = text[:cut], text[cut:]
        if len(head) < 4 or len(tail) < 4:
            break
        shots[idx] = target.model_copy(update={"narration": head, "est_sec": _estimate(head, rate)})
        shots.insert(
            idx + 1,
            target.model_copy(update={"narration": tail, "est_sec": _estimate(tail, rate)}),
        )
    return shots


def _visual_prompt(narration: str, idx: int, partition: str) -> str:
    clean = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", narration)
    subject = clean[:16] or "抽象画面"
    style = STYLES.get(partition, STYLES["知识"])
    composition = COMPOSITIONS[idx % len(COMPOSITIONS)]
    return f"{subject}，{style}，{composition}，16:9，画面无文字"[:120]


@register(Stage.STORYBOARD)
def run(ctx: StageContext) -> StageResult:
    try:
        script = Script.model_validate(ctx.upstream or {})
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "上游脚本非法", detail={"errors": exc.errors()[:3]}) from exc

    rate = script.tts_rate or ctx.settings.tts_rate
    partition = str((ctx.upstream or {}).get("partition") or ctx.partition or "知识")

    shots: list[Shot] = []
    for seg in script.segments:
        for text in chunk_text(seg.text):
            shots.append(
                Shot(
                    idx=0,
                    segment_idx=seg.idx,
                    narration=text,
                    visual_prompt="",
                    motion="kenburns_in",
                    kind_hint="image",
                    est_sec=_estimate(text, rate),
                )
            )

    shots = _merge_to_max(shots)
    shots = _split_to_min(shots, rate)

    if not MIN_SHOTS <= len(shots) <= MAX_SHOTS:
        raise AppError(ErrCode.E_VALIDATION, f"镜头数 {len(shots)} 不在 {MIN_SHOTS}–{MAX_SHOTS}")

    final: list[Shot] = []
    for i, shot in enumerate(shots):
        final.append(
            shot.model_copy(
                update={
                    "idx": i,
                    "visual_prompt": _visual_prompt(shot.narration, i, partition),
                    "motion": MOTIONS[i % len(MOTIONS)],
                }
            )
        )

    storyboard = Storyboard(shots=final)
    drift = abs(sum(s.est_sec for s in final) + 0.8 - script.total_sec) / max(script.total_sec, 1e-6)
    if drift > 0.05:
        raise AppError(
            ErrCode.E_VALIDATION,
            f"分镜总时长与脚本偏差 {drift:.1%} > 5%",
            detail={"total": script.total_sec, "shots": sum(s.est_sec for s in final)},
        )

    return StageResult(
        data=storyboard.model_dump(),
        meta={"shots": len(final), "drift": round(drift, 4), "total_sec": script.total_sec},
    )


@register_validator(Stage.STORYBOARD)
def validate(data: dict) -> list[Check]:
    shots = data.get("shots") or []
    return [
        Check(name="shot_count", passed=MIN_SHOTS <= len(shots) <= MAX_SHOTS, detail=f"{len(shots)} 镜"),
        Check(
            name="narration_nonempty",
            passed=all(len(str(s.get("narration") or "")) >= 4 for s in shots),
            detail="每镜必须有旁白",
        ),
        Check(
            name="visual_prompt_present",
            passed=all(6 <= len(str(s.get("visual_prompt") or "")) <= 120 for s in shots),
            detail="每镜必须有 6–120 字的画面描述",
        ),
        Check(
            name="duration_positive",
            passed=all(float(s.get("est_sec") or 0) > 0 for s in shots),
            detail="每镜时长必须为正",
        ),
    ]
