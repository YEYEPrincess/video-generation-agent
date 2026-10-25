"""M3 RENDER：小尺寸快速跑通 + 结构校验 + 时长兜底。

用 320x180 / 10fps / 3 镜 1.2s 的样本跑完整流程（约 3 秒），
避免在单测里渲染 1080p（那要 90 秒）。
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
import subprocess
from PIL import Image, ImageChops, ImageStat

from app.contracts import Assets, AssetShot, Audio, AudioShot, Shot, Storyboard
from app.core.artifacts import write
from app.core.context import StageContext
from app.core.queue import Queue
from app.core.state_machine import Stage
from app.errors import AppError
from app.stages import render

SHOTS = 3
DUR = 1.2


def _wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * int(24000 * seconds))


def seed_artifacts(conn, settings, shots: int = SHOTS) -> str:
    q = Queue(conn, settings)
    task_id = q.enqueue("渲染测试")
    root = Path(settings.artifacts_dir)

    media = root / task_id / "ASSETS" / "media"
    media.mkdir(parents=True, exist_ok=True)
    asset_list = []
    for i in range(shots):
        path = media / f"shot_{i:02d}.png"
        Image.new("RGB", (320, 180), (30 + 40 * i, 40, 60)).save(path)
        asset_list.append(
            AssetShot(idx=i, kind="image", path=(Path(task_id) / "ASSETS" / "media" / path.name).as_posix(),
                      width=320, height=180, provider="placeholder")
        )

    audio_dir = root / task_id / "VOICE" / "media"
    audio_dir.mkdir(parents=True, exist_ok=True)
    per_shot = []
    for i in range(shots):
        path = audio_dir / f"shot_{i:02d}.wav"
        _wav(path, DUR)
        per_shot.append(
            AudioShot(idx=i, path=(Path(task_id) / "VOICE" / "media" / path.name).as_posix(), dur_sec=DUR)
        )

    srt = root / task_id / "VOICE" / "audio.srt"
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,200\n第一句\n\n"
        "2\n00:00:01,200 --> 00:00:02,400\n第二句\n\n"
        "3\n00:00:02,400 --> 00:00:03,600\n第三句\n",
        encoding="utf-8",
    )

    storyboard = Storyboard(
        shots=[
            Shot(idx=i, segment_idx=i, narration=f"第{i}句旁白内容。", visual_prompt="画面描述",
                 motion="none", est_sec=DUR)
            for i in range(shots)
        ]
    )
    assets = Assets(shots=asset_list, total_cost_cents=0, degraded_count=0)
    audio = Audio(per_shot=per_shot, srt_path=(Path(task_id) / "VOICE" / "audio.srt").as_posix(),
                  total_dur=round(DUR * shots, 2))

    write(conn, settings, task_id, Stage.STORYBOARD, storyboard.model_dump())
    write(conn, settings, task_id, Stage.ASSETS, assets.model_dump())
    write(conn, settings, task_id, Stage.VOICE, audio.model_dump())
    return task_id


def fast_settings(settings):
    return settings.model_copy(
        update={
            "render_width": 320,
            "render_height": 180,
            "render_fps": 10,
            "render_preset": "ultrafast",
            "ffmpeg_timeout_sec": 120,
        }
    )


def make_ctx(task_id: str, settings, conn) -> StageContext:
    return StageContext(task_id=task_id, topic="渲染测试", partition="知识", stage=Stage.RENDER,
                        upstream={}, attempt=1, settings=settings, conn=conn)


def test_render_produces_playable_video(conn, settings):
    fast = fast_settings(settings)
    task_id = seed_artifacts(conn, fast)
    result = render.run(make_ctx(task_id, fast, conn))

    data = result.data
    assert data["has_audio"] is True
    assert data["black_segments"] == 0
    assert data["silence_segments"] == 0
    assert data["width"] == 320 and data["height"] == 180
    assert data["fps"] == 10
    assert data["duration_sec"] > 5  # 3.6s 音频 + 8s 上限补足
    assert 0.9 < data["peak_db"] or data["peak_db"] <= 0.0

    video = Path(fast.artifacts_dir) / data["path"]
    assert video.exists() and video.stat().st_size > 10_000


def test_render_rejects_mismatched_shots(conn, settings):
    fast = fast_settings(settings)
    task_id = seed_artifacts(conn, fast)
    # 人为把音频砍掉一段，制造镜头数不一致
    audio_row = conn.execute(
        "SELECT path FROM artifacts WHERE task_id=? AND stage='VOICE'", (task_id,)
    ).fetchone()
    audio_path = Path(fast.artifacts_dir) / audio_row["path"]
    import json

    payload = json.loads(audio_path.read_text(encoding="utf-8"))
    payload["per_shot"] = payload["per_shot"][:-1]
    audio_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(AppError) as exc:
        render.run(make_ctx(task_id, fast, conn))
    assert exc.value.code.value == "E_VALIDATION"


def test_render_accepts_missing_asset_file(conn, settings):
    """素材文件丢失时应报 E_VALIDATION（不可重试），而不是让 ffmpeg 崩。"""
    fast = fast_settings(settings)
    task_id = seed_artifacts(conn, fast)
    (Path(fast.artifacts_dir) / task_id / "ASSETS" / "media" / "shot_01.png").unlink()

    with pytest.raises(AppError) as exc:
        render.run(make_ctx(task_id, fast, conn))
    assert exc.value.code.value == "E_VALIDATION"


# ------------------------------------------------------------------ 运镜回归
def _frame(video: Path, at: float, out: Path) -> Image.Image:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at),
         "-i", str(video), "-frames:v", "1", str(out)],
        check=True, capture_output=True,
    )
    return Image.open(out).convert("L")


def _pattern(path: Path) -> Path:
    """有结构的图（纯色图看不出推近，必须画点东西）。"""
    from PIL import ImageDraw

    img = Image.new("RGB", (320, 180), (30, 40, 60))
    draw = ImageDraw.Draw(img)
    for i in range(0, 320, 40):
        draw.line([(i, 0), (i, 180)], fill=(200, 120, 80), width=2)
    for j in range(0, 180, 30):
        draw.line([(0, j), (320, j)], fill=(90, 160, 200), width=2)
    draw.rectangle([120, 60, 200, 120], fill=(240, 240, 240))
    img.save(path)
    return path


def test_kenburns_really_moves(tmp_path, settings):
    """回归：zoompan 的 d 必须等于总帧数，否则 zoom 每帧重置 → 画面静止。"""
    from app.agent.tools.render import build_clip

    image = _pattern(tmp_path / "still.png")
    clip = tmp_path / "clip.mp4"
    build_clip(image, clip, 2.0, motion="kenburns_in", width=320, height=180, fps=10,
               preset="ultrafast", cwd=tmp_path, timeout=120)

    first = _frame(clip, 0.1, tmp_path / "f0.png")
    last = _frame(clip, 1.8, tmp_path / "f1.png")
    diff = ImageStat.Stat(ImageChops.difference(first, last)).mean[0]
    assert diff > 3.0, f"推近没有生效（帧差 {diff:.2f}）"


def test_static_motion_stays_static(tmp_path, settings):
    from app.agent.tools.render import build_clip

    image = _pattern(tmp_path / "still2.png")
    clip = tmp_path / "static.mp4"
    build_clip(image, clip, 2.0, motion="none", width=320, height=180, fps=10,
               preset="ultrafast", cwd=tmp_path, timeout=120)

    first = _frame(clip, 0.1, tmp_path / "s0.png")
    last = _frame(clip, 1.8, tmp_path / "s1.png")
    diff = ImageStat.Stat(ImageChops.difference(first, last)).mean[0]
    assert diff < 1.0, f"静态镜头不该有运动（帧差 {diff:.2f}）"
