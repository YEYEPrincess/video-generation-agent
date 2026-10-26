"""同一主题，三种编排方式跑一遍，对照延迟/产出/失败模式。

    python scripts/compare_engines.py --topic "为什么年轻人开始重看老动画"

对照的是**编排层**，不是模型：
- native   ：app/core/runner 手写状态机 + 阶段 handler
- langgraph：StateGraph + MemorySaver，节点复用同一套 prompt/校验
- sdk      ：OpenAI Agents SDK，用 function_tool 让模型"调用"保存函数
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.context import StageContext  # noqa: E402
from app.core.state_machine import Stage  # noqa: E402
from app.settings import Settings  # noqa: E402


def run_native(settings: Settings, topic: str) -> dict:
    from app.stages import research, script

    def ctx(stage: Stage, upstream: dict | None = None) -> StageContext:
        return StageContext(task_id="cmp", topic=topic, partition="知识", stage=stage,
                            upstream=upstream or {}, attempt=1, settings=settings, conn=None)  # type: ignore[arg-type]

    started = time.time()
    brief = research.run(ctx(Stage.RESEARCH))
    script_out = script.run(ctx(Stage.SCRIPT, brief.data))
    return {
        "seconds": round(time.time() - started, 2),
        "angle": brief.data["angle"],
        "duration": script_out.data["total_sec"],
        "chars": sum(len(s["text"]) for s in script_out.data["segments"]),
        "error": "",
    }


def run_langgraph(settings: Settings, topic: str) -> dict:
    from app.agent.graph_engine import run_research

    out = run_research(settings, topic, thread_id="cmp")
    brief = out.get("brief") or {}
    script_payload = out.get("script") or {}
    return {
        "seconds": out["seconds"],
        "angle": brief.get("angle", ""),
        "duration": script_payload.get("total_sec", 0),
        "chars": sum(len(s["text"]) for s in script_payload.get("segments", [])),
        "error": out.get("error", ""),
        "nodes": out.get("trace", []),
    }


def run_sdk(settings: Settings, topic: str) -> dict:
    from pydantic import ValidationError

    from app.agent.sdk_engine import run_research, validate_brief

    started = time.time()
    out = run_research(settings, topic)
    brief = out.get("brief") or {}
    usage = out.get("usage")
    try:
        validate_brief(brief)
        valid, why = True, ""
    except (ValidationError, Exception) as exc:  # noqa: BLE001
        valid, why = False, str(exc).splitlines()[0][:80]
    return {
        "seconds": round(time.time() - started, 2),
        "angle": brief.get("angle", ""),
        "angle_len": len(brief.get("angle", "")),
        "hooks": len(brief.get("hooks") or []),
        "duration": None,
        "chars": None,
        "tokens": getattr(usage, "total_tokens", None),
        "contract_valid": valid,
        "contract_error": why,
        "error": "" if brief else "tool 未被调用",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default="为什么年轻人开始重看老动画")
    ap.add_argument("--skip", default="", help="逗号分隔，跳过 sdk/langgraph")
    args = ap.parse_args()

    settings = Settings()
    if not settings.llm_api_key:
        print("没有 LLM_API_KEY，跑不了真实对照")
        return 1
    print(f"model={settings.llm_model}  topic={args.topic}\n")

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    runners = [("native", run_native), ("langgraph", run_langgraph), ("sdk", run_sdk)]
    results = {}
    for name, fn in runners:
        if name in skip:
            continue
        try:
            results[name] = fn(settings, args.topic)
            r = results[name]
            print(f"[{name:<9}] {r['seconds']:>6}s  angle={str(r.get('angle'))[:34]!r}  "
                  f"error={r.get('error') or '-'}")
        except Exception as exc:  # noqa: BLE001
            results[name] = {"error": f"{type(exc).__name__}: {exc}", "seconds": 0}
            print(f"[{name:<9}] FAILED: {type(exc).__name__}: {exc}")

    print("\n=== JSON ===")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
