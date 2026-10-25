"""M6 图生视频：协议、轮询、超时降级、素材阶段升级、渲染兼容。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.agent.tools import i2v
from app.contracts import Assets, Audio, AudioShot, Shot, Storyboard
from app.core.artifacts import write
from app.core.context import StageContext
from app.core.queue import Queue
from app.core.state_machine import Stage
from app.stages import assets, render


def _png(path: Path, color=(40, 60, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), color).save(path)
    return path


# ------------------------------------------------------------------ 工具层
def test_kenburns_fallback_produces_real_video(tmp_path, settings):
    from app.agent.tools.media import probe

    image = _png(tmp_path / "a.png")
    result = i2v.generate(
        image=image, prompt="测试画面", out_path=tmp_path / "a.mp4", duration=1.5, settings=settings
    )
    path = Path(result.path)
    assert path.exists() and path.stat().st_size > 1000
    assert result.provider == "kenburns"
    assert result.degraded is False, "运动图片是可用产物，不该触发 QC 重做"
    assert probe(path)["duration"] >= 1.0
    assert 1.0 <= result.duration_sec <= 2.5


def test_api_success_path(tmp_path, settings):
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    image = _png(tmp_path / "b.png")
    video_file = tmp_path / "remote.mp4"
    i2v._kenburns(image, video_file, 1.2, settings)  # 先造一个可下载的真视频

    # 起一个静态文件服务器冒充 CDN
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/remote.mp4"

    calls = {"post": 0, "get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(200, json={"id": "job-1"})
        calls["get"] += 1
        if calls["get"] < 2:
            return httpx.Response(200, json={"status": "processing"})
        return httpx.Response(200, json={"status": "succeeded", "url": url})

    api = settings.model_copy(
        update={"video_api_key": "k", "video_base_url": "https://api.example.com/v1", "i2v_poll_max": 5}
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = i2v.generate(
            image=image, prompt="p", out_path=tmp_path / "out.mp4", duration=1.2, settings=api, client=client
        )
    finally:
        server.shutdown()

    assert result.provider == "api"
    assert calls["post"] == 1 and calls["get"] >= 2
    assert Path(result.path).stat().st_size > 1000
    assert Path(result.path).read_bytes()[:4] != b"\x00\x00\x00\x00"


def test_api_failure_falls_back(tmp_path, settings):
    image = _png(tmp_path / "c.png")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    api = settings.model_copy(
        update={"video_api_key": "k", "video_base_url": "https://api.example.com/v1"}
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = i2v.generate(
        image=image, prompt="p", out_path=tmp_path / "fallback.mp4", duration=1.0, settings=api, client=client
    )
    assert result.provider == "kenburns"
    assert Path(result.path).exists()


def test_api_timeout_falls_back(tmp_path, settings):
    image = _png(tmp_path / "d.png")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job-2"})
        return httpx.Response(200, json={"status": "processing"})

    api = settings.model_copy(
        update={
            "video_api_key": "k",
            "video_base_url": "https://api.example.com/v1",
            "i2v_poll_max": 2,
            "i2v_timeout_sec": 1,
        }
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = i2v.generate(
        image=image, prompt="p", out_path=tmp_path / "timeout.mp4", duration=1.0, settings=api, client=client
    )
    assert result.provider == "kenburns"


# ------------------------------------------------------------------ 素材阶段
def test_assets_upgrades_first_shot_to_video(conn, settings):
    storyboard = Storyboard(
        shots=[
            Shot(idx=i, segment_idx=i, narration=f"第{i}句旁白内容。", visual_prompt="画面描述",
                 kind_hint="image", est_sec=1.2)
            for i in range(4)
        ]
    )
    task_id = Queue(conn, settings).enqueue("i2v 测试")
    i2v_on = settings.model_copy(update={"enable_i2v": True})
    ctx = StageContext(task_id=task_id, topic="t", partition="知识", stage=Stage.ASSETS,
                       upstream=storyboard.model_dump(), attempt=1, settings=i2v_on, conn=conn)
    result = assets.run(ctx)

    assert result.meta["upgraded_to_video"] == [0]
    kinds = [s["kind"] for s in result.data["shots"]]
    assert kinds[0] == "video"
    assert all(k == "image" for k in kinds[1:])
    video_path = Path(settings.artifacts_dir) / result.data["shots"][0]["path"]
    assert video_path.exists() and video_path.suffix == ".mp4"


def test_render_handles_video_asset(conn, settings):
    storyboard = Storyboard(
        shots=[
            Shot(idx=i, segment_idx=i, narration=f"第{i}句旁白内容。", visual_prompt="画面描述",
                 motion="none", est_sec=1.2)
            for i in range(3)
        ]
    )
    task_id = Queue(conn, settings).enqueue("视频素材渲染")
    root = Path(settings.artifacts_dir)
    media = root / task_id / "ASSETS" / "media"
    _png(media / "shot_00.png")
    _png(media / "shot_01.png")
    _png(media / "shot_02.png")

    video = media / "shot_00.mp4"
    i2v.generate(image=media / "shot_00.png", prompt="p", out_path=video, duration=1.2, settings=settings)

    asset_list = [
        {"idx": 0, "kind": "video", "path": f"{task_id}/ASSETS/media/shot_00.mp4", "width": 320, "height": 180,
         "provider": "kenburns"},
        {"idx": 1, "kind": "image", "path": f"{task_id}/ASSETS/media/shot_01.png", "width": 320, "height": 180},
        {"idx": 2, "kind": "image", "path": f"{task_id}/ASSETS/media/shot_02.png", "width": 320, "height": 180},
    ]

    import wave

    audio_dir = root / task_id / "VOICE" / "media"
    audio_dir.mkdir(parents=True, exist_ok=True)
    per_shot = []
    for i in range(3):
        p = audio_dir / f"shot_{i:02d}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(b"\x00\x00" * int(24000 * 1.2))
        per_shot.append({"idx": i, "path": f"{task_id}/VOICE/media/{p.name}", "dur_sec": 1.2})

    srt = root / task_id / "VOICE" / "audio.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,200\n第一句\n", encoding="utf-8")

    write(conn, settings, task_id, Stage.STORYBOARD, storyboard.model_dump())
    write(conn, settings, task_id, Stage.ASSETS, Assets(shots=asset_list).model_dump())
    write(conn, settings, task_id, Stage.VOICE,
          Audio(per_shot=[AudioShot(**s) for s in per_shot],
                srt_path=f"{task_id}/VOICE/audio.srt", total_dur=3.6).model_dump())

    ctx = StageContext(task_id=task_id, topic="t", partition="知识", stage=Stage.RENDER,
                       upstream={}, attempt=1, settings=settings, conn=conn)
    result = render.run(ctx)
    assert result.data["duration_sec"] > 5
    assert result.data["has_audio"] is True
