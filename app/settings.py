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

    # --- 模型（M1 起使用） ---
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_sec: float = 60.0
    llm_max_tokens: int = 1200
    llm_temperature: float = 0.7
    llm_json_mode: bool = True
    #: 成本估算：每 1k tokens 多少分（人民币分）。按自己的套餐填，0 表示不统计
    llm_price_cents_per_1k: float = 0.0
    #: 强制使用 MockLLM（离线开发 / CI）
    mock_llm: bool = False

    # --- 图像生成（M2） ---
    img_provider: str = "auto"  # auto / api / placeholder
    img_base_url: str = ""
    img_api_key: str = ""
    img_model: str = "dall-e-3"
    img_size: str = "1792x1024"
    img_timeout_sec: float = 90.0
    #: 单张图成本（分），用于预算统计
    img_cost_cents: int = 0
    assets_concurrency: int = 3

    # --- 语音合成（M2） ---
    tts_provider: str = "auto"  # auto / edge / silent
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    tts_timeout_sec: float = 45.0

    # --- 视频生成（M6） ---
    video_api_key: str = ""

    # --- 文本节奏 ---
    #: 实测：edge-tts 中文女声约 4.6–4.7 字/秒。按 4.2 估会偏长 11%，
    #: 导致成片掉到 45s 以下。改 prompt 字数区间时要同步复核这里。
    tts_rate: float = 4.6
