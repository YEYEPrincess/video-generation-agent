"""M2 阶段：ASSETS 并发出图与降级、VOICE 实测时长与 SRT。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts import Audio
from app.core.context import StageContext
from app.core.queue import Queue
from app.core.runner import run_task
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages import assets, research, script, storyboard, voice


def make_ctx(stage: Stage, upstream: dict | None = None, settings=None, task_id="t_m2") -> StageContext:
    return StageContext(
        task_id=task_id,
        topic="为什么年轻人开始重看老动画",
        partition="知识",
        stage=stage,
        upstream=upstream or {},
        attempt=1,
        settings=settings,
        conn=None,
    )


def storyboard_of(settings, task_id="t_m2") -> dict:
    brief = research.run(make_ctx(Stage.RESEARCH, settings=settings, task_id=task_id)).data
    script_data = script.run(make_ctx(Stage.SCRIPT, brief, settings, task_id)).data
    return storyboard.run(make_ctx(Stage.STORYBOARD, script_data, settings, task_id)).data


# ------------------------------------------------------------------ ASSETS
def test_assets_generates_every_shot(settings):
    sb = storyboard_of(settings)
    result = assets.run(make_ctx(Stage.ASSETS, sb, settings))

    assert result.validate_contract is True
    assert len(result.data["shots"]) == len(sb["shots"])
    assert result.meta["prompt_mode"] == "llm"
    assert result.data["degraded_count"] == 0

    for shot in result.data["shots"]:
        path = Path(settings.artifacts_dir) / shot["path"]
        assert path.exists(), f"missing asset {path}"
        assert path.suffix == ".png"
        assert shot["provider"] == "placeholder"


def test_assets_falls_back_when_prompt_rewrite_fails(settings, monkeypatch):
    sb = storyboard_of(settings)

    def boom(*args, **kwargs):
        raise AppError(ErrCode.E_VALIDATION, "模型没给够画面描述")

    monkeypatch.setattr("app.stages.assets.rewrite_prompts", boom)
    result = assets.run(make_ctx(Stage.ASSETS, sb, settings))

    assert result.meta["prompt_mode"] == "fallback"
    assert len(result.data["shots"]) == len(sb["shots"]), "降级后仍要出全部镜头"


def test_assets_marks_degraded_when_image_fails(settings, monkeypatch):
    sb = storyboard_of(settings)

    from app.agent.tools.images import ImageResult

    def degrade(*, prompt, narration, out_path, size, index, settings):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"not-a-real-png")
        return ImageResult(path=str(out_path), width=1920, height=1080, provider="placeholder", degraded=True)

    monkeypatch.setattr("app.stages.assets.generate", degrade)
    result = assets.run(make_ctx(Stage.ASSETS, sb, settings))
    assert result.data["degraded_count"] == len(sb["shots"])


# ------------------------------------------------------------------ VOICE
def test_srt_format():
    text = voice.build_srt([(3.5, "第一句"), (2.0, "第二句")])
    lines = text.strip().splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:03,500"
    assert lines[2] == "第一句"
    assert "00:00:03,500 --> 00:00:05,500" in text


def test_voice_end_to_end(conn, settings):
    q = Queue(conn, settings)
    tid = q.enqueue("为什么年轻人开始重看老动画")
    assert q.lease("w2") is not None
    assert run_task(conn, tid, "w2", settings) == "DONE"

    row = conn.execute("SELECT path FROM artifacts WHERE task_id=? AND stage='VOICE'", (tid,)).fetchone()
    data = json.loads((Path(settings.artifacts_dir) / row["path"]).read_text(encoding="utf-8"))
    audio = Audio.model_validate(data)

    assert len(audio.per_shot) >= 3
    assert audio.total_dur > 0
    assert abs(sum(s.dur_sec for s in audio.per_shot) - audio.total_dur) < 0.05

    srt = Path(settings.artifacts_dir) / audio.srt_path
    assert srt.exists()
    body = srt.read_text(encoding="utf-8")
    assert "-->" in body and body.count("\n\n") >= len(audio.per_shot) - 1

    for item in audio.per_shot:
        assert (Path(settings.artifacts_dir) / item.path).exists()


def test_voice_requires_storyboard(conn, settings):
    """没有分镜产物时，VOICE 必须报 E_VALIDATION 而不是崩。"""
    ctx = make_ctx(Stage.VOICE, settings=settings, task_id="t_missing")
    ctx.conn = conn
    with pytest.raises(AppError) as exc:
        voice.run(ctx)
    assert exc.value.code is ErrCode.E_VALIDATION
