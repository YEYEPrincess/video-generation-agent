"""译制流水线：把六步串起来，并留下可复查的产物。

每一步都落盘一个 JSON（asr / translation / clips / report），
这样出问题时能直接回答"是听错了、翻错了，还是没塞进时间轴"——
而不是只看到一条跑不通的片子。

    video.mp4
      │  extract_audio        ffmpeg 抽 16k 单声道
      ▼
    audio.wav ──► transcribe（whisper）──► segments[{start,end,zh}]
                                              │ translate（LLM，整段一次）
                                              ▼
                                          segments[+en]
                                              │ synthesize（edge-tts，目标语）
                                              ▼
                                          clips[mp3]
                                              │ fit（atempo 压进 slot）+ place（adelay 定位）
                                              ▼
                                          dubbed.m4a + subs.srt
                                              │ mux（换音轨 + 烧字幕）
                                              ▼
                                          final.mp4 → QC
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from app.agent.tools.media import probe
from app.agent.tools.render import mux_with_subtitles
from app.dub import qc as qc_mod
from app.dub.asr import transcribe
from app.dub.media import build_track, extract_audio, fit_clip, place_clips
from app.dub.subs import build_srt, read_srt_count
from app.dub.subcover import detect_band
from app.dub.translate import translate
from app.dub.tts import synthesize_segments
from app.dub.types import DubResult, Segment
from app.errors import AppError, ErrCode
from app.settings import Settings

DUB_ROOT = Path("artifacts_dub")


@dataclass
class Step:
    name: str
    seconds: float = 0.0
    detail: dict = field(default_factory=dict)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # default=str：Clip.path 是 Path，json 不认识；统一转字符串即可
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _report_md(result: DubResult, steps: list[Step], run_dir: Path) -> str:
    lines = [
        "# 译制报告",
        "",
        f"- 成片：`{result.video}`",
        f"- 字幕：`{result.srt}`",
        f"- 音轨：`{result.audio}`",
        "",
        "## 各步骤耗时",
        "",
        "| 步骤 | 秒 | 说明 |",
        "|---|---|---|",
    ]
    for step in steps:
        detail = ", ".join(f"{k}={v}" for k, v in step.detail.items())
        lines.append(f"| {step.name} | {step.seconds:.1f} | {detail} |")

    m = result.metrics
    lines += [
        "",
        "## 质量指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 原片时长 / 成片时长 | {m['src_duration']}s / {m['out_duration']}s |",
        f"| 句子数 / 字幕条数 | {m['segments']} / {m['srt_lines']} |",
        f"| 配音总时长 | {m['speech_seconds']}s |",
        f"| 最大加速倍数 | {m['max_speed']}x |",
        f"| 最大溢出 | {m['max_overflow']}s |",
        f"| 尾部漂移 | {m['tail_drift']}s |",
        f"| 是否通过 | {'✅' if m['passed'] else '❌'} |",
        "",
        "## 逐句对照（前 12 条）",
        "",
        "| # | 时间 | 原文 | 译文 |",
        "|---|---|---|---|",
    ]
    for seg, clip in list(zip(result.segments, result.clips))[:12]:
        lines.append(
            f"| {seg.idx} | {clip.placed_at:.1f}–{clip.placed_at + clip.duration:.1f}s | "
            f"{seg.text} | {seg.translated} |"
        )
    text = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text


def run(video: Path, settings: Settings, glossary: dict[str, str] | None = None,
        keep_work: bool = True, cover_override: tuple[int, int] | None = None) -> DubResult:
    video = Path(video).resolve()
    if not video.exists():
        raise AppError(ErrCode.E_VALIDATION, f"视频不存在: {video}", retryable=False)

    run_id = f"{datetime.now():%Y%m%d-%H%M%S}_{video.stem[:10]}"
    run_dir = (DUB_ROOT / run_id).resolve()
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=True)

    steps: list[Step] = []
    src = probe(video)
    if not src["has_audio"]:
        raise AppError(ErrCode.E_VALIDATION, "源视频没有音轨，无法译制", retryable=False)

    # 1) 抽音轨
    t0 = time.time()
    wav = extract_audio(video, work / "src.wav", settings.ffmpeg_timeout_sec)
    steps.append(Step("extract_audio", round(time.time() - t0, 1), {"duration": round(src["duration"], 2)}))

    # 2) 语音识别
    t0 = time.time()
    segments = transcribe(wav, settings)
    if not segments:
        raise AppError(ErrCode.E_VALIDATION, "没有识别到任何语音", retryable=False)
    _write_json(run_dir / "asr.json", [asdict(s) for s in segments])
    steps.append(Step("transcribe", round(time.time() - t0, 1),
                      {"model": settings.dub_asr_model, "segments": len(segments)}))

    # 3) 翻译
    t0 = time.time()
    segments = translate(segments, settings, glossary=glossary)
    _write_json(run_dir / "translation.json", [asdict(s) for s in segments])
    steps.append(Step("translate", round(time.time() - t0, 1),
                      {"target": settings.dub_target_lang, "chars": sum(len(s.translated) for s in segments)}))

    # 4) 配音
    t0 = time.time()
    clips = synthesize_segments(segments, work, settings)
    steps.append(Step("synthesize", round(time.time() - t0, 1), {"clips": len(clips)}))

    # 5) 时间轴：先压进 slot，再决定落点
    t0 = time.time()
    fitted = [fit_clip(c, s.slot, settings, work) for s, c in zip(segments, clips)]
    placed, drift = place_clips(segments, fitted)
    _write_json(run_dir / "clips.json", [asdict(c) for c in placed])
    steps.append(Step("align", round(time.time() - t0, 1),
                      {"max_speed": max(c.speed for c in placed), "drift": drift}))

    # 6) 混音 + 字幕
    t0 = time.time()
    track = build_track(placed, src["duration"], work / "dubbed.m4a", settings)
    srt = build_srt(segments, placed, work / "subs.srt")
    steps.append(Step("mix+subs", round(time.time() - t0, 1), {"track": track.name}))

    # 7) 合流：盖住原字幕 → 换音轨 → 烧英文字幕
    t0 = time.time()
    shutil.copyfile(video, work / "src.mp4")
    band = cover_override or detect_band(work / "src.mp4")
    # ⚠️ libass 处理 SRT 时用 384×288 的虚拟坐标系，MarginV 会被放大 H/288 倍。
    # 直接写像素值，字幕会被顶到画面上半部分（我们踩过这个坑）。
    band_bottom = band[0] + band[1]
    margin = max(10, int((settings.render_height - band_bottom + 12) * 288 / settings.render_height))
    mux_with_subtitles(
        "src.mp4", track.name, srt.name, "final.mp4",
        width=settings.render_width, height=settings.render_height,
        preset=settings.render_preset, cwd=work, timeout=settings.ffmpeg_timeout_sec,
        duration=None, cover=band, margin_v=margin,
    )
    out = work / "final.mp4"
    final_dur = round(probe(out)["duration"], 2)
    steps.append(Step("mux", round(time.time() - t0, 1),
                      {"duration": final_dur, "subtitle_band": f"y={band[0]},h={band[1]}"}))

    # 8) 质检
    metrics = qc_mod.check(segments, placed, src["duration"], final_dur, read_srt_count(srt))
    steps.append(Step("qc", 0.0, {"passed": metrics["passed"]}))

    final_copy = run_dir / "final.mp4"
    shutil.copyfile(out, final_copy)
    srt_copy = run_dir / "subs.en.srt"
    shutil.copyfile(srt, srt_copy)

    result = DubResult(video=final_copy, srt=srt_copy, audio=run_dir / "dubbed.m4a",
                       segments=segments, clips=placed, metrics=metrics)
    shutil.copyfile(track, result.audio)
    _report_md(result, steps, run_dir)
    _write_json(run_dir / "dub.json", {
        "video": str(final_copy), "srt": str(srt_copy),
        "metrics": metrics, "steps": [asdict(s) for s in steps],
    })

    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    return result
