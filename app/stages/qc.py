"""QC 阶段：4 条 DoD 的可自动判定部分 + **回退路由**。

它不是"打分器"，是"路由器"：只回答三个问题
1. 这条片子能不能发（DONE）
2. 有没有具体某几镜是坏的（RETRY，指回 ASSETS）
3. 坏在结构上、重做也没用（PARTIAL，并写清原因）

能自动判的只有：时长、黑帧/静音、字幕时间轴、画面是否"平"（纯色/黑图）。
"镜头与旁白语义相关"要靠 CLIP 或人评，这里只把降级镜头挑出来。
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageStat
from pydantic import ValidationError

from app.contracts import Assets, Audio, Check, QCReport, Storyboard, Video
from app.core.artifacts import load as load_artifact
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages.registry import register
from app.stages.validators import register as register_validator

MIN_SEC, MAX_SEC = 45.0, 90.0

_TS = re.compile(r"-->?\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _srt_last_end(path: Path) -> float | None:
    """取最后一条字幕的**结束**时间。

    ⚠️ 这里曾经取到的是"最后一条的开始时间"，导致 QC 误判字幕没覆盖到结尾。
    """
    if not path.exists():
        return None
    matches = _TS.findall(path.read_text(encoding="utf-8", errors="replace"))
    if not matches:
        return None
    hours, minutes, seconds, millis = matches[-1]
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def _image_problem(path: Path, min_bytes: int, min_stddev: float) -> str | None:
    """纯色/黑图/坏文件 → 返回原因；正常返回 None。"""
    if not path.exists():
        return "missing"
    if path.stat().st_size < min_bytes:
        return f"too_small({path.stat().st_size}B)"
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            stddev = ImageStat.Stat(gray).stddev[0]
            if stddev < min_stddev:
                return f"flat(stddev={stddev:.1f})"
    except Exception as exc:  # noqa: BLE001
        return f"decode_error({exc.__class__.__name__})"
    return None


@register(Stage.QC)
def run(ctx: StageContext) -> StageResult:
    settings = ctx.settings
    root = Path(settings.artifacts_dir).resolve()
    try:
        video = Video.model_validate(load_artifact(ctx.conn, settings, ctx.task_id, Stage.RENDER))
        assets = Assets.model_validate(load_artifact(ctx.conn, settings, ctx.task_id, Stage.ASSETS))
        audio = Audio.model_validate(load_artifact(ctx.conn, settings, ctx.task_id, Stage.VOICE))
        Storyboard.model_validate(load_artifact(ctx.conn, settings, ctx.task_id, Stage.STORYBOARD))
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "QC 缺少上游产物", detail={"errors": exc.errors()[:3]}) from exc

    if len(assets.shots) != len(audio.per_shot):
        raise AppError(
            ErrCode.E_VALIDATION,
            f"素材 {len(assets.shots)} 与音频 {len(audio.per_shot)} 数量不一致",
            retryable=False,
        )

    checks: list[Check] = []

    # DoD 1：时长
    checks.append(
        Check(
            name="duration_in_range",
            passed=settings.qc_min_sec <= video.duration_sec <= settings.qc_max_sec,
            detail=f"{video.duration_sec}s ∈ [{settings.qc_min_sec},{settings.qc_max_sec}]",
        )
    )
    # DoD 2：黑帧 / 片中静音 / 爆音
    checks.append(Check(name="no_black", passed=video.black_segments == 0, detail=f"{video.black_segments} 段"))
    checks.append(Check(name="no_silence", passed=video.silence_segments == 0, detail=f"{video.silence_segments} 段"))
    checks.append(
        Check(name="no_clipping", passed=video.peak_db < -0.05, detail=f"peak={video.peak_db}dB（接近 0 即削波）")
    )
    checks.append(
        Check(
            name="audio_loud_enough",
            passed=video.peak_db > -30.0,
            severity="warn",
            detail=f"peak={video.peak_db}dB" + ("" if video.peak_db > -30.0 else "，偏轻"),
        )
    )

    # DoD 3：字幕（对齐的是**音频**，不是被拉长的视频）
    last_end = _srt_last_end(root / audio.srt_path)
    expected_end = audio.total_dur or video.duration_sec
    checks.append(Check(name="subtitle_exists", passed=last_end is not None, detail=str(audio.srt_path)))
    checks.append(
        Check(
            name="subtitle_covers_audio",
            passed=last_end is not None and abs(expected_end - last_end) <= 2.0,
            detail=f"srt 结束 {last_end} vs 音频 {expected_end}",
        )
    )

    # DoD 4：镜头画面质量（能自动查的那部分）
    bad_shots: list[tuple[int, str]] = []
    fallback_video: list[int] = []
    for shot in assets.shots:
        path = root / shot.path
        if shot.kind == "video":
            problem = None if path.exists() and path.stat().st_size > settings.qc_min_bytes else "video_missing"
            if shot.provider == "kenburns":
                fallback_video.append(shot.idx)
        else:
            problem = _image_problem(path, settings.qc_min_bytes, settings.qc_min_stddev)
        if problem:
            bad_shots.append((shot.idx, problem))
        if shot.degraded:
            bad_shots.append((shot.idx, "degraded"))

    checks.append(
        Check(
            name="shot_quality",
            passed=not bad_shots,
            detail="; ".join(f"#{i}:{why}" for i, why in bad_shots) or "全部正常",
        )
    )
    checks.append(
        Check(
            name="audio_not_degraded",
            passed=audio.degraded_count == 0,
            severity="warn",
            detail=f"{audio.degraded_count} 段为静音兜底",
        )
    )
    checks.append(
        Check(
            name="i2v_effective",
            passed=not fallback_video,
            severity="warn",
            detail=f"第 {fallback_video} 镜未用上视频生成，已回落运动图片" if fallback_video else "ok",
        )
    )

    rounds = int(ctx.conn.execute("SELECT qc_rounds FROM tasks WHERE id = ?", (ctx.task_id,)).fetchone()[0])
    blocking = [c for c in checks if c.severity == "block" and not c.passed]
    non_shot = [c for c in blocking if c.name != "shot_quality"]
    retry_shots = sorted({idx for idx, _ in bad_shots})

    if retry_shots and not non_shot and rounds < settings.qc_max_rounds:
        decided = "RETRY"
        reason = f"第 {retry_shots} 镜画面异常，回退重做"
    elif blocking:
        decided = "PARTIAL"
        reason = "; ".join(f"{c.name}({c.detail})" for c in blocking)
    else:
        decided = "DONE"
        reason = None

    score = max(0, 100 - 15 * len(blocking) - 5 * len([c for c in checks if not c.passed and c.severity == "warn"]))
    report = QCReport(
        checks=checks,
        retry_shots=retry_shots if decided == "RETRY" else [],
        score=score,
        degraded_reason=reason,
        decided=decided,  # type: ignore[arg-type]
    )

    return StageResult(
        data=report.model_dump(),
        meta={
            "rounds": rounds,
            "blocking": [c.name for c in blocking],
            "retry_shots": retry_shots,
            "decided": decided,
        },
    )


@register_validator(Stage.QC)
def validate(data: dict) -> list[Check]:
    checks = data.get("checks") or []
    decided = data.get("decided")
    consistent = (decided == "RETRY") == bool(data.get("retry_shots"))
    return [
        Check(name="checks_present", passed=len(checks) >= 4, detail=f"{len(checks)} 项检查"),
        Check(name="decision_valid", passed=decided in {"DONE", "PARTIAL", "RETRY", "NEEDS_HUMAN"}, detail=str(decided)),
        Check(name="retry_consistent", passed=consistent, detail="RETRY 必须带 retry_shots，反之亦然"),
    ]
