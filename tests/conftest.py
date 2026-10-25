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
    )


@pytest.fixture()
def conn(settings):
    c = connect(settings.db_path)
    init_db(c)
    yield c
    c.close()
