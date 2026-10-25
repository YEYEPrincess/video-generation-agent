"""M2 工具层：占位图、静音兜底、媒体探测。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.agent.tools.images import generate, make_placeholder
from app.agent.tools.media import probe
from app.agent.tools.tts import synthesize
from app.settings import Settings


def test_placeholder_image_size_and_provider(tmp_path, settings):
    result = generate(
        prompt="窗边的书桌，扁平插画",
        narration="一段用来测试的旁白文字",
        out_path=tmp_path / "shot_00.png",
        size=(960, 540),
        index=0,
        settings=settings,
    )
    path = Path(result.path)
    assert path.exists()
    with Image.open(path) as img:
        assert img.size == (960, 540)
    assert result.width == 960 and result.height == 540
    assert result.provider == "placeholder"
    assert result.degraded is False


def test_placeholder_deterministic_per_prompt(tmp_path, settings):
    a = make_placeholder("同一个 prompt", "旁白", tmp_path / "a.png", (320, 180), 0)
    b = make_placeholder("同一个 prompt", "旁白", tmp_path / "b.png", (320, 180), 0)
    assert Path(a.path).read_bytes() == Path(b.path).read_bytes()


def test_api_provider_without_key_uses_placeholder(tmp_path):
    settings = Settings(_env_file=None, img_provider="api", img_api_key="", img_base_url="")
    result = generate(
        prompt="x",
        narration="没有 key 时的旁白",
        out_path=tmp_path / "shot_01.png",
        size=(320, 180),
        index=1,
        settings=settings,
    )
    assert result.provider == "placeholder"
    assert result.degraded is False


def test_silent_tts_duration_matches_estimate(tmp_path, settings):
    result = synthesize("这是一段用来测试时长的旁白，大概二十来个字。", tmp_path, 0, settings)
    assert result.provider == "silent"
    assert result.degraded is True
    assert 1.5 <= result.duration_sec <= 30
    info = probe(result.path)
    assert info["has_audio"] is True
    assert abs(info["duration"] - result.duration_sec) < 0.15


def test_probe_missing_file_raises(tmp_path):
    from app.errors import AppError

    try:
        probe(tmp_path / "nope.wav")
    except AppError as exc:
        assert exc.code.value == "E_RENDER"
    else:  # pragma: no cover
        raise AssertionError("should raise")


def test_tts_creates_output_dir_before_edge(tmp_path, monkeypatch, settings):
    """回归：edge_tts.save 不会建目录，必须先 mkdir，否则第 1 镜必静音。"""
    seen: dict[str, bool] = {}

    async def fake_save(text, out_path, voice, timeout):  # noqa: ANN001
        seen["dir_exists"] = Path(out_path).parent.exists()
        raise RuntimeError("stop before network")

    monkeypatch.setattr("app.agent.tools.tts._edge_save", fake_save)
    edge_settings = settings.model_copy(update={"tts_provider": "edge"})
    result = synthesize("测试一句", tmp_path / "deep" / "nested", 0, edge_settings)

    assert seen["dir_exists"] is True
    assert result.provider == "silent"  # 失败后仍降级，不抛异常
