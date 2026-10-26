"""把视频流水线暴露成 MCP 工具，让 Claude Desktop / Cursor 这类客户端直接调用。

    python -m app.mcp_server          # stdio 传输，给 MCP 客户端用

客户端配置（Claude Desktop 的 claude_desktop_config.json）示例：

    {
      "mcpServers": {
        "bili-video-agent": {
          "command": "D:\\\\Desktop\\\\video-generation agent\\\\.venv\\\\Scripts\\\\python.exe",
          "args": ["-m", "app.mcp_server"],
          "cwd": "D:\\\\Desktop\\\\video-generation agent"
        }
      }
    }

设计原则：MCP 只做**薄封装**——参数校验、短返回、错误说人话。
真正的编排仍在 worker 里，不要在工具内部跑长任务（MCP 调用会超时）。
"""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from app.core.queue import Queue
from app.core.state_machine import Stage
from app.db import connect, init_db
from app.settings import Settings

server = MCPServer(
    name="bili-video-agent",
    instructions="输入主题生成 45–90 秒视频；任务异步执行，用 task_status 轮询，用 video_path 取成片。",
)


def _queue() -> tuple[Queue, object]:
    settings = Settings()
    conn = connect(settings.db_path)
    init_db(conn)
    return Queue(conn, settings), conn


@server.tool()
def submit_video(topic: str, partition: str = "知识") -> str:
    """提交一条视频生成任务（异步）。返回 task_id，用它查询状态。"""
    if not topic.strip():
        raise ValueError("topic 不能为空")
    q, conn = _queue()
    try:
        task_id = q.enqueue(topic.strip(), partition)
        return json.dumps({"task_id": task_id, "status": "PENDING"}, ensure_ascii=False)
    finally:
        conn.close()  # type: ignore[attr-defined]


@server.tool()
def task_status(task_id: str) -> str:
    """查询任务状态、当前阶段、进度与耗时。"""
    q, conn = _queue()
    try:
        task = q.get(task_id)
        if task is None:
            return json.dumps({"error": f"task not found: {task_id}"}, ensure_ascii=False)
        stages = q.stages(task_id)
        done = sum(1 for s in stages if s["status"] in {"DONE", "SKIPPED"})
        return json.dumps(
            {
                "task_id": task_id,
                "status": task["status"],
                "current_stage": task["current_stage"],
                "progress": f"{done}/{len(stages)}",
                "error": task["error"],
                "updated_at": task["updated_at"],
            },
            ensure_ascii=False,
        )
    finally:
        conn.close()  # type: ignore[attr-defined]


@server.tool()
def list_recent_tasks(limit: int = 10) -> str:
    """列出最近的任务（id / 主题 / 状态）。"""
    q, conn = _queue()
    try:
        rows = conn.execute(  # type: ignore[attr-defined]
            "SELECT id, topic, status, created_at FROM tasks ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()
        return json.dumps([dict(r) for r in rows], ensure_ascii=False)
    finally:
        conn.close()  # type: ignore[attr-defined]


@server.tool()
def video_path(task_id: str) -> str:
    """取成片的绝对路径。没渲染完会返回提示，不要反复重试。"""
    settings = Settings()
    q, conn = _queue()
    try:
        task = q.get(task_id)
        if task is None:
            return json.dumps({"error": f"task not found: {task_id}"}, ensure_ascii=False)
        if task["status"] not in {"DONE", "PARTIAL"}:
            return json.dumps({"status": task["status"], "hint": "任务未完成，先用 task_status 查看阶段"},
                              ensure_ascii=False)
        row = conn.execute(  # type: ignore[attr-defined]
            "SELECT path FROM artifacts WHERE task_id=? AND stage=? ORDER BY version DESC LIMIT 1",
            (task_id, Stage.RENDER.value),
        ).fetchone()
        if row is None:
            return json.dumps({"error": "没有渲染产物"}, ensure_ascii=False)
        meta = json.loads((Path(settings.artifacts_dir) / row["path"]).read_text(encoding="utf-8"))
        return json.dumps(
            {"video": str((Path(settings.artifacts_dir) / meta["path"]).resolve()),
             "duration_sec": meta.get("duration_sec")},
            ensure_ascii=False,
        )
    finally:
        conn.close()  # type: ignore[attr-defined]


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
