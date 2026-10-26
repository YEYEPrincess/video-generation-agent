"""框架适配器的单元测试：不联网，用 MockLLM 跑通编排层。"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.agent import graph_engine
from app.agent.llm import MockLLM


def test_langgraph_graph_completes_with_mock(settings, monkeypatch):
    monkeypatch.setattr(graph_engine, "get_llm", lambda s: MockLLM())
    out = graph_engine.run_research(settings, "为什么年轻人开始重看老动画", thread_id="t1")

    assert out["error"] == "", f"图执行失败: {out['error']}"
    assert out["brief"]["hooks"]
    assert 45.0 <= out["script"]["total_sec"] <= 90.0
    assert out["trace"][:3] == ["plan", "write", "check"]


def test_langgraph_route_retries_on_bad_output(settings, monkeypatch):
    """模型第一次给坏 JSON，图应该回到 plan 再来一轮。"""

    class FlakyLLM(MockLLM):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        def complete(self, **kwargs):  # noqa: ANN003
            self.n += 1
            if self.n == 1:
                from app.agent.llm import LLMResult

                return LLMResult(text="not json at all", tokens=1)
            return super().complete(**kwargs)

    monkeypatch.setattr(graph_engine, "get_llm", lambda s: FlakyLLM())
    out = graph_engine.run_research(settings, "测试重试", thread_id="t2")
    assert out["trace"].count("plan") >= 2
    assert out["brief"], "第二轮应该成功"


def test_sdk_contract_rejects_overlong_angle():
    from app.agent.sdk_engine import validate_brief

    good = {"angle": "从社交货币角度解释老番翻红", "hooks": ["一", "二", "三"], "keywords": ["a", "b", "c"]}
    assert validate_brief(good).angle

    with pytest.raises(ValidationError):
        validate_brief({**good, "angle": "太" * 50})


def test_mcp_tools_registered():
    from app import mcp_server

    tools = asyncio.run(mcp_server.server.list_tools())
    names = {t.name for t in tools}
    assert {"submit_video", "task_status", "list_recent_tasks", "video_path"} <= names
    for tool in tools:
        assert tool.description, f"{tool.name} 缺少描述（模型选工具全靠它）"
