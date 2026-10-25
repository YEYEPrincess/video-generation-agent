from __future__ import annotations

import pytest

from app.db import connect, init_db
from app.settings import Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        db_path=str(tmp_path / "test.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
        fake_mode=True,
        fake_stage_sleep=0.0,
        stage_backoff_sec=(0.0, 0.0, 0.0),
        lease_sec=5,
        # 测试一律走本地实现：不联网、不花钱、结果确定
        img_provider="placeholder",
        tts_provider="silent",
        assets_concurrency=2,
        img_size="960x540",
        # 渲染也用小尺寸：单测里跑 1080p 一次要 90s
        render_width=320,
        render_height=180,
        render_fps=10,
        render_preset="ultrafast",
        ffmpeg_timeout_sec=120,
        # 测试样本只有 3~4 秒，质检的时长门槛要跟着放宽
        qc_min_sec=1.0,
        qc_max_sec=120.0,
        # 图生视频是可选增强：默认关，M6 的用例单独打开
        enable_i2v=False,
    )


@pytest.fixture()
def conn(settings):
    c = connect(settings.db_path)
    init_db(c)
    yield c
    c.close()
