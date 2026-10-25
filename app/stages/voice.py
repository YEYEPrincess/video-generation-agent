"""VOICE 阶段：逐镜配音 + 字幕时间轴。

关键点：**时长用实测值，不用估算**。配音快慢因人因地而异，
按字数估的时长会让字幕和画面对不上——那正是 DoD 里最先崩的一条。

注意：本阶段的输入是 STORYBOARD（要旁白原文），不是上一个阶段的 ASSETS。
状态机顺序只是执行顺序，不等于数据依赖顺序。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.agent.tools.tts import synthesize
from app.contracts import Audio, AudioShot, Check, Storyboard
from app.core.artifacts import load as load_artifact
from app.core.artifacts import stage_dir
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.core.trace import emit
from app.errors import AppError, ErrCode
from app.stages.registry import register
from app.stages.validators import register as register_validator


def _ts(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(entries: list[tuple[float, str]]) -> str:
    """entries: [(duration, text)] → SRT 文本（时间轴累加）。"""
    lines: list[str] = []
    cursor = 0.0
    for index, (duration, text) in enumerate(entries, start=1):
        lines.append(str(index))
        lines.append(f"{_ts(cursor)} --> {_ts(cursor + duration)}")
        lines.append(text)
        lines.append("")
        cursor += duration
    return "\n".join(lines)


def _relative(settings, abs_path: str) -> str:
    return Path(abs_path).relative_to(Path(settings.artifacts_dir)).as_posix()


@register(Stage.VOICE)
def run(ctx: StageContext) -> StageResult:
    raw = load_artifact(ctx.conn, ctx.settings, ctx.task_id, Stage.STORYBOARD)
    try:
        storyboard = Storyboard.model_validate(raw or {})
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "找不到可用的分镜产物", detail={"errors": exc.errors()[:3]}) from exc

    media_dir = stage_dir(ctx.settings, ctx.task_id, Stage.VOICE) / "media"
    per_shot: list[AudioShot] = []
    entries: list[tuple[float, str]] = []
    providers: set[str] = set()
    degraded = 0

    for shot in storyboard.shots:
        result = synthesize(shot.narration, media_dir, shot.idx, ctx.settings)
        providers.add(result.provider)
        degraded += 1 if result.degraded else 0
        per_shot.append(
            AudioShot(idx=shot.idx, path=_relative(ctx.settings, result.path), dur_sec=result.duration_sec)
        )
        entries.append((result.duration_sec, shot.narration))

    srt_path = stage_dir(ctx.settings, ctx.task_id, Stage.VOICE) / "audio.srt"
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(build_srt(entries), encoding="utf-8")

    total = round(sum(item.dur_sec for item in per_shot), 2)
    audio = Audio(
        per_shot=per_shot,
        srt_path=_relative(ctx.settings, str(srt_path)),
        total_dur=total,
        voice=ctx.settings.tts_voice,
        alignment="estimate",
        degraded_count=degraded,
    )

    if degraded:
        emit(
            ctx.conn,
            ctx.task_id,
            "tool_result",
            stage=Stage.VOICE.value,
            level="warn",
            payload={"degraded": degraded, "providers": sorted(providers)},
        )

    return StageResult(
        data=audio.model_dump(),
        meta={"providers": sorted(providers), "shots": len(per_shot), "total_dur": total, "degraded": degraded},
    )


@register_validator(Stage.VOICE)
def validate(data: dict) -> list[Check]:
    per_shot = data.get("per_shot") or []
    total = float(data.get("total_dur") or 0)
    summed = round(sum(float(item.get("dur_sec") or 0) for item in per_shot), 2)
    return [
        Check(name="audio_count", passed=len(per_shot) > 0, detail=f"{len(per_shot)} 段音频"),
        Check(
            name="duration_positive",
            passed=all(float(item.get("dur_sec") or 0) > 0 for item in per_shot),
            detail="每段音频时长必须为正",
        ),
        Check(
            name="total_consistent",
            passed=abs(summed - total) <= 0.05,
            detail=f"分段合计 {summed}s vs 总时长 {total}s",
        ),
    ]
