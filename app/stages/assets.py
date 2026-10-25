"""ASSETS 阶段：批量改写画面描述 + 并发生成图片。

两个设计点：
1. **先批量重写 prompt，再生成图**：STORYBOARD 的 visual_prompt 是代码拼的，
   只有"主体+风格"，喂给 T2I 会画出文字碎片。这里用一次模型调用把它们
   改写成「主体+动作+场景+风格+景别」——一次调用换 6 张图的质量，很划算。
2. **单镜失败不拖垮整条**：任何一镜失败都回落占位图并标 `degraded`，
   任务是 `PARTIAL` 而不是 `FAILED`。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import ValidationError

from app.agent.llm import extract_json, get_llm, stage_marker
from app.agent.prompts import render
from app.agent.tools.images import generate
from app.contracts import AssetShot, Assets, Check, Shot, Storyboard
from app.core.artifacts import stage_dir
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.settings import Settings
from app.stages.registry import register
from app.stages.validators import register as register_validator

SYSTEM = stage_marker("ASSETS") + "\n你是视频分镜美术指导，输出必须是可解析的 JSON。"
STYLE_HINT = "扁平插画，深蓝与暖橙撞色，柔和侧光，干净背景，16:9"


def _parse_size(raw: str) -> tuple[int, int]:
    try:
        width, height = raw.lower().split("x")
        return int(width), int(height)
    except ValueError:
        return 1920, 1080


def rewrite_prompts(shots: list[Shot], settings: Settings) -> tuple[list[str], int, int]:
    """一次调用改写全部镜头。返回 (prompts, tokens, cost)。"""
    llm = get_llm(settings)
    lines = "\n".join(f"{i + 1}. {shot.narration}" for i, shot in enumerate(shots))
    user = render("assets", shots=lines, count=len(shots), style=STYLE_HINT)
    result = llm.complete(system=SYSTEM, user=user, max_tokens=900)

    raw = extract_json(result.text)
    prompts = [str(p).strip() for p in (raw.get("prompts") or [])]
    if len(prompts) != len(shots):
        raise AppError(ErrCode.E_VALIDATION, f"画面描述数量 {len(prompts)} != 镜头数 {len(shots)}")
    if not all(6 <= len(p) <= 120 for p in prompts):
        raise AppError(ErrCode.E_VALIDATION, "画面描述长度需在 6–120 字之间")
    return prompts, result.tokens, result.cost_cents


def _relative(settings: Settings, abs_path: str) -> str:
    return Path(abs_path).relative_to(Path(settings.artifacts_dir)).as_posix()


@register(Stage.ASSETS)
def run(ctx: StageContext) -> StageResult:
    try:
        storyboard = Storyboard.model_validate(ctx.upstream or {})
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "上游分镜非法", detail={"errors": exc.errors()[:3]}) from exc

    shots = storyboard.shots
    tokens = cost = 0
    prompt_mode = "llm"
    try:
        prompts, tokens, cost = rewrite_prompts(shots, ctx.settings)
    except AppError as exc:
        # 改写失败不致命：退回分镜自带的画面描述，仍然能出图
        prompts = [shot.visual_prompt for shot in shots]
        prompt_mode = "fallback"
        ctx.settings  # 保持签名可读
        last_error = exc.message
    else:
        last_error = ""

    media_dir = stage_dir(ctx.settings, ctx.task_id, Stage.ASSETS) / "media"
    size = _parse_size(ctx.settings.img_size)

    def _one(index: int, shot: Shot, prompt: str):
        return generate(
            prompt=prompt,
            narration=shot.narration,
            out_path=media_dir / f"shot_{index:02d}.png",
            size=size,
            index=index,
            settings=ctx.settings,
        )

    with ThreadPoolExecutor(max_workers=max(1, ctx.settings.assets_concurrency)) as pool:
        futures = [pool.submit(_one, i, shot, prompt) for i, (shot, prompt) in enumerate(zip(shots, prompts))]
        results = [f.result() for f in futures]

    asset_shots: list[AssetShot] = []
    for index, (result, prompt) in enumerate(zip(results, prompts)):
        asset_shots.append(
            AssetShot(
                idx=index,
                kind="image",
                path=_relative(ctx.settings, result.path),
                width=result.width,
                height=result.height,
                provider=result.provider,
                cost_cents=result.cost_cents,
                degraded=result.degraded,
            )
        )

    assets = Assets(
        shots=asset_shots,
        total_cost_cents=sum(s.cost_cents for s in asset_shots),
        degraded_count=sum(1 for s in asset_shots if s.degraded),
    )

    return StageResult(
        data=assets.model_dump(),
        tokens=tokens,
        cost_cents=cost + assets.total_cost_cents,
        meta={
            "prompt_mode": prompt_mode,
            "prompt_error": last_error,
            "degraded": assets.degraded_count,
            "provider": asset_shots[0].provider if asset_shots else "none",
        },
    )


@register_validator(Stage.ASSETS)
def validate(data: dict) -> list[Check]:
    shots = data.get("shots") or []
    degraded = sum(1 for s in shots if s.get("degraded"))
    return [
        Check(name="assets_count", passed=len(shots) > 0, detail=f"{len(shots)} 个镜头素材"),
        Check(
            name="paths_present",
            passed=all(str(s.get("path") or "").strip() for s in shots),
            detail="每个镜头必须有素材路径",
        ),
        Check(
            name="degraded_consistent",
            passed=int(data.get("degraded_count") or 0) == degraded,
            detail=f"degraded_count={data.get('degraded_count')} 与实际 {degraded} 不一致",
        ),
        Check(
            name="size_ok",
            passed=all(int(s.get("width") or 0) >= 720 for s in shots),
            detail="素材宽度需 ≥720",
        ),
    ]
