"""配置收口：所有可变参数从这里进，禁止散落在业务代码里。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 允许 Settings(_env_file=None) 在测试里彻底隔离环境变量
        case_sensitive=False,
    )

    # --- 存储 ---
    db_path: str = "app.db"
    artifacts_dir: str = "artifacts"

    # --- 队列 / 租约 ---
    lease_sec: int = 60
    heartbeat_sec: int = 10
    reclaim_sec: int = 15
    poll_sec: float = 1.0
    max_attempt: int = 3

    # --- 预算 ---
    budget_cents: int = 80

    # --- 阶段 ---
    stage_timeout_sec: int = 120
    stage_backoff_sec: tuple[float, ...] = (1.0, 3.0, 9.0)

    # --- M0：假阶段（M1 起由真实 handler 接管） ---
    fake_mode: bool = True
    fake_stage_sleep: float = 0.5

    # --- 模型 / 多模态（M1 起使用） ---
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    img_api_key: str = ""
    video_api_key: str = ""
