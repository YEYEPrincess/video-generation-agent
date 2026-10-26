"""LangGraph 版编排：用 StateGraph + Checkpointer 重写「调研 → 脚本 → 校验」。

存在的意义是**对照**，不是替换：
- 原生实现（app/core/runner.py）：状态机 + 幂等键 + 落库，控制流在你自己手里；
- LangGraph：节点/边/检查点由框架托管，写起来快，但失败语义要重新理解。

这里刻意复用同一套 prompt、同一个 LLM 客户端和后端校验器，
这样对照实验才只差"编排层"这一个变量。
"""

from __future__ import annotations

import time
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.llm import extract_json, get_llm, stage_marker
from app.agent.prompts import render
from app.contracts import ResearchBrief, Script
from app.core.state_machine import Stage
from app.errors import AppError
from app.settings import Settings
from app.stages.script import build_script, fit_to_max, MAX_SEC, MIN_SEC
from app.stages.validators import validate as run_validators

MAX_ROUNDS = 2


class GraphState(TypedDict, total=False):
    topic: str
    partition: str
    brief: dict[str, Any]
    script: dict[str, Any]
    rounds: int
    error: str
    trace: list[str]


def _blocking(stage: Stage, data: dict[str, Any]) -> list[str]:
    return [c.name for c in run_validators(stage, data) if c.severity == "block" and not c.passed]


def build_graph(settings: Settings):
    """编译一张 3 节点的小图：plan → write → check（失败回到 plan）。"""
    llm = get_llm(settings)

    def plan(state: GraphState) -> GraphState:
        trace = state.get("trace", []) + ["plan"]
        user = render("research", topic=state["topic"], partition=state.get("partition") or "知识")
        res = llm.complete(system=stage_marker("RESEARCH") + "\n只输出 JSON。", user=user, max_tokens=800)
        try:
            brief = ResearchBrief.model_validate(extract_json(res.text))
        except (AppError, Exception) as exc:  # noqa: BLE001
            return {"trace": trace, "error": f"plan: {exc}", "rounds": state.get("rounds", 0) + 1}
        return {"trace": trace, "brief": brief.model_dump(), "error": "", "rounds": state.get("rounds", 0) + 1}

    def write(state: GraphState) -> GraphState:
        trace = state.get("trace", []) + ["write"]
        if state.get("error") or not state.get("brief"):
            return {"trace": trace}
        brief = ResearchBrief.model_validate(state["brief"])
        user = render("script", angle=brief.angle, hooks="；".join(brief.hooks),
                      keywords="、".join(brief.keywords))
        if state.get("error"):
            user += f"\n\n【上一版被打回】{state['error']}"
        res = llm.complete(system=stage_marker("SCRIPT") + "\n只输出 JSON。", user=user, max_tokens=1000)
        try:
            script = fit_to_max(build_script(extract_json(res.text), settings.tts_rate))
            if not MIN_SEC <= script.total_sec <= MAX_SEC:
                raise AppError("E_VALIDATION", f"时长越界 {script.total_sec}s")  # type: ignore[arg-type]
        except AppError as exc:
            return {"trace": trace, "error": f"write: {exc.message}"}
        return {"trace": trace, "script": script.model_dump(), "error": ""}

    def check(state: GraphState) -> GraphState:
        trace = state.get("trace", []) + ["check"]
        if state.get("error"):
            return {"trace": trace}
        problems = _blocking(Stage.RESEARCH, state["brief"]) + _blocking(Stage.SCRIPT, state["script"])
        if problems:
            return {"trace": trace, "error": "validators: " + ",".join(problems)}
        return {"trace": trace, "error": ""}

    def route(state: GraphState) -> Literal["plan", "end"]:
        if state.get("error") and state.get("rounds", 0) < MAX_ROUNDS:
            return "plan"
        return "end"

    graph = StateGraph(GraphState)
    graph.add_node("plan", plan)
    graph.add_node("write", write)
    graph.add_node("check", check)
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "write")
    graph.add_edge("write", "check")
    graph.add_conditional_edges("check", route, {"plan": "plan", "end": END})
    return graph.compile(checkpointer=MemorySaver())


def run_research(settings: Settings, topic: str, partition: str = "知识",
                 thread_id: str = "t1") -> dict[str, Any]:
    """跑一次，返回 {brief, script, trace, error, seconds}。"""
    app = build_graph(settings)
    state = {"topic": topic, "partition": partition, "rounds": 0, "error": "", "trace": []}
    started = time.time()
    out = app.invoke(state, config={"configurable": {"thread_id": thread_id}})
    out["seconds"] = round(time.time() - started, 2)
    return out
