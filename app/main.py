"""FastAPI 应用工厂。

用工厂函数（而不是模块级 app）是为了测试能注入临时 db / artifacts 目录。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

from app import __version__
from app.api.routes import router
from app.db import connect, init_db
from app.settings import Settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(s.db_path)
        init_db(conn)
        app.state.conn = conn
        app.state.settings = s
        Path(s.artifacts_dir).mkdir(parents=True, exist_ok=True)
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(title="B站 AI 视频创作平台 · 任务编排 Agent", version=__version__, lifespan=lifespan)
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def index():
        if not (WEB_DIR / "index.html").exists():
            return RedirectResponse(url="/docs")
        return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
