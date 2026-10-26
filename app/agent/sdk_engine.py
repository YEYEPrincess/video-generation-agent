"""OpenAI Agents SDK 版：用 Agent + function_tool + Runner 重写「调研」这一步。

对照点：
- 原生实现里，模型输出是**一段 JSON 文本**，我们自己解析、自己校验；
- Agents SDK 里，模型直接调用 `save_brief(...)` 这个 tool，参数由 SDK 校验并结构化，
  我们拿到的就是 Python 对象——省掉"抠 JSON"这一整类失败。

代价：多一层框架依赖 + 一次额外的 tool round-trip（tokens 会多一点）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled
from openai import AsyncOpenAI

from app.contracts import ResearchBrief
from app.settings import Settings

# 我们用的是 DeepSeek，不是 OpenAI 官方端点；关掉 trace 上报避免噪音报错
set_tracing_disabled(True)

INSTRUCTIONS = """你是 B站知识区的选题策划。
给定主题，调用 save_brief 工具提交三个字段：
- angle：切入角度，10–40 字，必须具体（不要「介绍 XX」这种空话）
- hooks：3 个开场钩子，每个 12–30 字
- keywords：3–6 个检索关键词
必须调用工具，不要只用文字回答。"""


def build_agent(settings: Settings, sink: dict[str, Any]) -> Agent:
    @function_tool
    def save_brief(angle: str, hooks: list[str], keywords: list[str]) -> str:
        """保存选题简报。angle 是切入角度，hooks 是三个开场钩子，keywords 是检索关键词。"""
        sink.update({"angle": angle, "hooks": hooks, "keywords": keywords})
        return "已保存"

    client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    model = OpenAIChatCompletionsModel(model=settings.llm_model, openai_client=client)
    return Agent(
        name="选题策划",
        instructions=INSTRUCTIONS,
        model=model,
        tools=[save_brief],
    )


async def _run(settings: Settings, topic: str) -> dict[str, Any]:
    sink: dict[str, Any] = {}
    agent = build_agent(settings, sink)
    started = time.time()
    result = await Runner.run(agent, f"主题：{topic}")
    seconds = round(time.time() - started, 2)
    return {
        "brief": sink,
        "final": str(result.final_output)[:200],
        "seconds": seconds,
        "usage": getattr(getattr(result, "context_wrapper", None), "usage", None),
    }


def run_research(settings: Settings, topic: str) -> dict[str, Any]:
    """同步入口（CLI 用）。"""
    return asyncio.run(_run(settings, topic))


def validate_brief(payload: dict[str, Any]) -> ResearchBrief:
    """SDK 给的是结构化参数，但**契约校验不能省**——模型仍可能给出长度不合规的值。"""
    return ResearchBrief.model_validate(
        {**payload, "partition": "知识", "refs": [{"title": "（SDK 版未检索）", "ref": "n/a", "why": "占位"}]}
    )
