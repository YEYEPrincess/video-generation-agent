"""ASSETS 阶段：批量改写画面描述 + 并发生成图片（支持"只重做指定镜头"）。

回退重做时：
- `shots` 表里 status='RETRY' 的镜头才重新生成；
- 其余镜头直接复用上一版产物，不花一分钱、不占一秒；
- 这就是 QC 回退能做到"精准"的原因。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pydantic import ValidationError

from app.agent.llm import extract_json, get_llm, stage_marker
from app.agent.prompts import render
from app.agent.tools.images import generate
from app.contracts import AssetShot, Assets, Check, Shot, Storyboard
from app.core.artifacts import load as load_artifact
from app.core.artifacts import stage_dir
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.core.trace import emit
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
    return Path(abs_path).resolve().relative_to(Path(settings.artifacts_dir).resolve()).as_posix()


def _reusable(ctx: StageContext, root: Path) -> dict[int, AssetShot]:
    """上一版里仍然有效的镜头（文件在、状态 DONE）。"""
    previous = load_artifact(ctx.conn, ctx.settings, ctx.task_id, Stage.ASSETS)
    rows = {
        int(r["idx"]): dict(r)
        for r in ctx.conn.execute("SELECT * FROM shots WHERE task_id = ?", (ctx.task_id,))
    }
    reusable: dict[int, AssetShot] = {}
    for item in previous.get("shots") or []:
        idx = int(item["idx"])
        row = rows.get(idx)
        if row and row["status"] == "DONE" and (root / item["path"]).exists():
            reusable[idx] = AssetShot.model_validate(item)
    return reusable


@register(Stage.ASSETS)
def run(ctx: StageContext) -> StageResult:
    try:
        storyboard = Storyboard.model_validate(ctx.upstream or {})
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "上游分镜非法", detail={"errors": exc.errors()[:3]}) from exc

    settings = ctx.settings
    shots = storyboard.shots
    root = Path(settings.artifacts_dir).resolve()
    reusable = _reusable(ctx, root)
    todo = [i for i in range(len(shots)) if i not in reusable]

    tokens = cost = 0
    prompt_mode = "reuse"
    last_error = ""
    prompts: dict[int, str] = {}

    if todo:
        targets = [shots[i] for i in todo]
        try:
            generated, tokens, cost = rewrite_prompts(targets, settings)
            prompt_mode = "llm"
        except AppError as exc:
            generated = [shot.visual_prompt for shot in targets]
            prompt_mode = "fallback"
            last_error = exc.message
        prompts = dict(zip(todo, generated))

    media_dir = stage_dir(settings, ctx.task_id, Stage.ASSETS) / "media"
    size = _parse_size(settings.img_size)

    def _one(index: int):
        return generate(
            prompt=prompts[index],
            narration=shots[index].narration,
            out_path=media_dir / f"shot_{index:02d}.png",
            size=size,
            index=index,
            settings=settings,
        )

    generated_results: dict[int, object] = {}
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, settings.assets_concurrency)) as pool:
            futures = {i: pool.submit(_one, i) for i in todo}
            for i, future in futures.items():
                generated_results[i] = future.result()

    asset_shots: list[AssetShot] = []
    for index in range(len(shots)):
        if index in reusable:
            asset_shots.append(reusable[index])
            continue
        result = generated_results[index]
        asset_shots.append(
            AssetShot(
                idx=index,
                kind="image",
                path=_relative(settings, result.path),
                width=result.width,
                height=result.height,
                provider=result.provider,
                cost_cents=result.cost_cents,
                degraded=result.degraded,
            )
        )

    # 高光镜升级为视频：素材阶段的"选材"决策，同样是确定性的（前 N 镜）
    upgraded: list[int] = []
    if settings.enable_i2v and len(asset_shots) > 2:
        from app.agent.tools.i2v import generate as generate_video

        for index in range(min(settings.i2v_max_shots, len(asset_shots))):
            shot = asset_shots[index]
            source = root / shot.path
            result = generate_video(
                image=source,
                prompt=prompts.get(index, shots[index].visual_prompt),
                out_path=media_dir / f"shot_{index:02d}.mp4",
                duration=shots[index].est_sec,
                settings=settings,
            )
            asset_shots[index] = shot.model_copy(
                update={
                    "kind": "video",
                    "path": _relative(settings, result.path),
                    "provider": result.provider,
                    "degraded": shot.degraded or result.degraded,
                }
            )
            upgraded.append(index)

    # 镜头级状态落表：QC 回退时只改这里，资产阶段据此决定重做谁
    for index, asset in enumerate(asset_shots):
        row = ctx.conn.execute(
            "SELECT retry_count FROM shots WHERE task_id = ? AND idx = ?", (ctx.task_id, index)
        ).fetchone()
        retry_count = int(row["retry_count"]) if row else 0
        ctx.conn.execute(
            "INSERT OR REPLACE INTO shots(task_id, idx, narration, visual_prompt, kind, asset_path,"
            " sim_score, status, retry_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ctx.task_id, index, shots[index].narration,
                prompts.get(index, shots[index].visual_prompt),
                "image", asset.path, asset.sim_score, "DONE", retry_count,
            ),
        )

    assets = Assets(
        shots=asset_shots,
        total_cost_cents=sum(s.cost_cents for s in asset_shots),
        degraded_count=sum(1 for s in asset_shots if s.degraded),
    )

    failures = [
        {"idx": idx, "error": result.error}
        for idx, result in sorted(generated_results.items())
        if getattr(result, "error", None)
    ]
    if failures:
        emit(
            ctx.conn,
            ctx.task_id,
            "tool_result",
            stage=Stage.ASSETS.value,
            level="warn",
            payload={"image_failures": failures},
        )

    return StageResult(
        data=assets.model_dump(),
        tokens=tokens,
        cost_cents=cost + assets.total_cost_cents,
        meta={
            "prompt_mode": prompt_mode,
            "prompt_error": last_error,
            "reused": sorted(reusable),
            "regenerated": sorted(todo),
            "upgraded_to_video": upgraded,
            "degraded": assets.degraded_count,
            "image_failures": failures,
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
