"""RENDER 阶段：把 6 张图 + 6 段配音合成一条能播的 mp4。

职责边界（面试可讲）：
- RENDER 只负责「产出一个可播放、音画同步的文件」，结构性问题（黑帧/静音）
  只记录不拦截——因为拦在这里等于整条任务没有产物；
- 质量判定交给 QC（M5），它可以把任务判成 PARTIAL 并指出"第几镜有问题"。

时间轴：每段画面时长 = 对应音频实测时长；音频总额不足 45s 时，最后一镜定格补足。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import ValidationError

from app.agent.tools.render import (
    build_clip,
    build_clip_from_video,
    concat_audio,
    concat_video,
    inspect,
    mux_with_subtitles,
    prepare_workdir,
    probe_basic,
)
from app.contracts import Assets, Audio, Check, Storyboard, Video
from app.core.artifacts import load as load_artifact
from app.core.artifacts import stage_dir
from app.core.context import StageContext, StageResult
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages.registry import register
from app.stages.validators import register as register_validator

MIN_SEC, MAX_SEC = 45.0, 90.0
MAX_PAD_SEC = 8.0


def _copy(src: Path, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


@register(Stage.RENDER)
def run(ctx: StageContext) -> StageResult:
    s = ctx.settings
    try:
        assets = Assets.model_validate(load_artifact(ctx.conn, s, ctx.task_id, Stage.ASSETS))
        audio = Audio.model_validate(load_artifact(ctx.conn, s, ctx.task_id, Stage.VOICE))
        storyboard = Storyboard.model_validate(load_artifact(ctx.conn, s, ctx.task_id, Stage.STORYBOARD))
    except ValidationError as exc:
        raise AppError(ErrCode.E_VALIDATION, "RENDER 缺少上游产物", detail={"errors": exc.errors()[:3]}) from exc

    if not (len(assets.shots) == len(audio.per_shot) == len(storyboard.shots)):
        raise AppError(
            ErrCode.E_VALIDATION,
            f"镜头数不一致：assets={len(assets.shots)} audio={len(audio.per_shot)} storyboard={len(storyboard.shots)}",
        )

    durations = {item.idx: item.dur_sec for item in audio.per_shot}
    total_audio = round(sum(durations.values()), 2)
    if total_audio > MAX_SEC:
        raise AppError(ErrCode.E_VALIDATION, f"音频总长 {total_audio}s 超过 {MAX_SEC}s")

    # 目标时长：不低于 45s；不足的部分由"最后一镜定格 + 音轨补静音"共同补足。
    # +0.3s 是拼接/编码的余量，否则 45.0 很容易被压成 44.97。
    target = round(max(MIN_SEC, total_audio), 2)
    pad = round(min(max(0.0, target + 0.3 - total_audio), MAX_PAD_SEC), 2)

    work = prepare_workdir(stage_dir(s, ctx.task_id, Stage.RENDER) / "work").resolve()
    artifacts_root = Path(s.artifacts_dir).resolve()

    clips: list[Path] = []
    audio_files: list[Path] = []
    for index in range(len(assets.shots)):
        shot = assets.shots[index]
        image_src = artifacts_root / shot.path
        audio_src = artifacts_root / audio.per_shot[index].path
        if not image_src.exists() or not audio_src.exists():
            raise AppError(ErrCode.E_VALIDATION, f"第 {index} 镜素材文件缺失", retryable=False)

        duration = durations[shot.idx] + (pad if index == len(assets.shots) - 1 else 0.0)
        clip = work / f"clip_{index:02d}.mp4"

        if shot.kind == "video":
            source = _copy(image_src, work / f"vid_{index:02d}{image_src.suffix}")
            build_clip_from_video(
                source, clip, round(duration, 3),
                width=s.render_width, height=s.render_height, fps=s.render_fps,
                preset=s.render_preset, cwd=work, timeout=s.ffmpeg_timeout_sec,
            )
        else:
            source = _copy(image_src, work / f"img_{index:02d}{image_src.suffix}")
            build_clip(
                source, clip, round(duration, 3),
                motion=storyboard.shots[index].motion,
                width=s.render_width, height=s.render_height, fps=s.render_fps,
                preset=s.render_preset, cwd=work, timeout=s.ffmpeg_timeout_sec,
            )

        audio_files.append(_copy(audio_src, work / f"a_{index:02d}{audio_src.suffix}"))
        clips.append(clip)

    concat_video(clips, "video.mp4", cwd=work, timeout=s.ffmpeg_timeout_sec)
    concat_audio(audio_files, "audio.m4a", cwd=work, timeout=s.ffmpeg_timeout_sec, pad_to=target)

    srt_src = artifacts_root / audio.srt_path
    if not srt_src.exists():
        raise AppError(ErrCode.E_VALIDATION, "字幕文件缺失", retryable=False)
    _copy(srt_src, work / "subs.srt")

    mux_with_subtitles(
        "video.mp4", "audio.m4a", "subs.srt", "final.mp4",
        width=s.render_width, height=s.render_height, preset=s.render_preset,
        cwd=work, timeout=s.ffmpeg_timeout_sec, duration=target,
    )

    final_abs = work / "final.mp4"
    final_rel = (Path(ctx.task_id) / Stage.RENDER.value / "final.mp4").as_posix()
    shutil.copyfile(final_abs, artifacts_root / final_rel)

    info = probe_basic(final_abs)
    detected = inspect(final_abs, cwd=work, timeout=s.ffmpeg_timeout_sec, duration=info["duration"])

    video = Video(
        path=final_rel,
        duration_sec=round(info["duration"], 2),
        size_bytes=info["size"],
        width=info["width"],
        height=info["height"],
        fps=int(round(info["fps"] or s.render_fps)),
        has_audio=bool(info["has_audio"]),
        black_segments=detected["black_segments"],
        silence_segments=detected["silence_segments"],
        peak_db=round(float(detected["peak_db"]), 1),
    )

    return StageResult(
        data=video.model_dump(),
        meta={
            "audio_total": total_audio,
            "padded": pad,
            "duration": video.duration_sec,
            "black": video.black_segments,
            "silence": video.silence_segments,
            "tail_silence": detected.get("tail_silence_sec", 0.0),
            "peak_db": video.peak_db,
        },
    )


@register_validator(Stage.RENDER)
def validate(data: dict) -> list[Check]:
    duration = float(data.get("duration_sec") or 0)
    return [
        Check(name="decodable", passed=duration > 0, detail=f"时长 {duration}s"),
        Check(name="has_audio", passed=bool(data.get("has_audio")), detail="必须带音轨"),
        Check(name="size_ok", passed=int(data.get("size_bytes") or 0) > 10_000, detail="文件过小，可能没写成功"),
        Check(
            name="resolution",
            passed=int(data.get("width") or 0) >= 320,
            severity="warn",
            detail=f"宽度 {data.get('width')}，成片建议 ≥1280",
        ),
        # 黑帧/静音只记录不拦截：拦截会让任务直接没有产物，交给 QC 判 PARTIAL
        Check(
            name="no_black",
            passed=int(data.get("black_segments") or 0) == 0,
            severity="warn",
            detail=f"黑帧段 {data.get('black_segments')}",
        ),
        Check(
            name="no_silence",
            passed=int(data.get("silence_segments") or 0) == 0,
            severity="warn",
            detail=f"静音段 {data.get('silence_segments')}",
        ),
    ]
