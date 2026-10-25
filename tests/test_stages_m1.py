"""M1 三个阶段：契约、时长约束、确定性截断、失败路径。"""

from __future__ import annotations

import json

import pytest

from app.agent.llm import LLMResult
from app.core.context import StageContext
from app.core.state_machine import Stage
from app.errors import AppError, ErrCode
from app.stages import research, script, storyboard


class ScriptedLLM:
    """按顺序吐出预置响应，用于测试"模型不听话"的各种情况。"""

    name = "scripted"

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls = 0

    def complete(self, **kwargs) -> LLMResult:  # noqa: ANN003
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), tokens=10, cost_cents=1)


def make_ctx(stage: Stage, upstream: dict | None = None, settings=None) -> StageContext:
    return StageContext(
        task_id="t_test",
        topic="为什么年轻人开始重看老动画",
        partition="知识",
        stage=stage,
        upstream=upstream or {},
        attempt=1,
        settings=settings,
        conn=None,  # 阶段 handler 不允许直接访问 DB
    )


BRIEF = {
    "partition": "知识",
    "angle": "从新观众与平台算法的双重视角，解释老番为什么重新流行",
    "hooks": ["你的首页是不是也被老番占领了", "十年前的分镜为什么更耐看", "谁在推这些老片"],
    "refs": [{"title": "老番考古", "ref": "BV1xx411c7mD", "why": "同类选题数据参考"}],
    "keywords": ["老番", "考古", "怀旧", "二创"],
}


# ------------------------------------------------------------------ RESEARCH
def test_research_produces_valid_brief(settings):
    result = research.run(make_ctx(Stage.RESEARCH, settings=settings))
    assert result.validate_contract is True
    assert len(result.data["hooks"]) == 3
    assert result.data["refs"]


def test_research_rejects_bad_contract(settings, monkeypatch):
    bad = ScriptedLLM([{"angle": "太短"}])
    monkeypatch.setattr("app.stages.research.get_llm", lambda s: bad)
    with pytest.raises(AppError) as exc:
        research.run(make_ctx(Stage.RESEARCH, settings=settings))
    assert exc.value.code is ErrCode.E_VALIDATION


# ------------------------------------------------------------------ SCRIPT
def test_script_duration_in_range(settings):
    result = script.run(make_ctx(Stage.SCRIPT, BRIEF, settings))
    assert 45.0 <= result.data["total_sec"] <= 90.0
    assert 3 <= len(result.data["segments"]) <= 10
    assert result.validate_contract is True


def test_script_truncates_overlong_output(settings, monkeypatch):
    long_text = "这是一段非常长的旁白" * 10  # 100 字，6 段共 600 字 ≈ 143s
    llm = ScriptedLLM([{"title": "标题", "hook": "钩子", "segments": [{"text": long_text}] * 6}])
    monkeypatch.setattr("app.stages.script.get_llm", lambda s: llm)
    result = script.run(make_ctx(Stage.SCRIPT, BRIEF, settings))
    assert result.data["total_sec"] <= 90.0
    assert result.meta["attempts"] == 1, "确定性截断应该一次成功，不该靠重试"


def test_script_rejects_too_short(settings, monkeypatch):
    llm = ScriptedLLM([{"title": "标题", "hook": "钩子", "segments": [{"text": "太短了吧。"}] * 3}])
    monkeypatch.setattr("app.stages.script.get_llm", lambda s: llm)
    with pytest.raises(AppError) as exc:
        script.run(make_ctx(Stage.SCRIPT, BRIEF, settings))
    assert exc.value.code is ErrCode.E_VALIDATION
    assert "45" in exc.value.message
    assert llm.calls == 2, "应该带反馈重试一次再放弃"


def test_script_retries_with_feedback(settings, monkeypatch):
    short = {"title": "标题", "hook": "钩子", "segments": [{"text": "太短了吧。"}] * 3}
    good = {
        "title": "老番为什么又火了",
        "hook": "你的首页被老番占领了吗",
        "segments": [{"text": "这是" + "很充实的一段口播内容" * 5} for _ in range(4)],
    }
    llm = ScriptedLLM([short, good])
    monkeypatch.setattr("app.stages.script.get_llm", lambda s: llm)
    result = script.run(make_ctx(Stage.SCRIPT, BRIEF, settings))
    assert result.meta["attempts"] == 2
    assert llm.calls == 2


# ------------------------------------------------------------------ STORYBOARD
def test_storyboard_from_script(settings):
    script_data = script.run(make_ctx(Stage.SCRIPT, BRIEF, settings)).data
    result = storyboard.run(make_ctx(Stage.STORYBOARD, script_data, settings))
    shots = result.data["shots"]
    assert 3 <= len(shots) <= 10
    assert all(shot["visual_prompt"] for shot in shots)
    assert all(shot["narration"] for shot in shots)
    total = sum(shot["est_sec"] for shot in shots) + 0.8
    assert abs(total - script_data["total_sec"]) / script_data["total_sec"] < 0.05


def test_storyboard_merges_too_many_shots(settings):
    segments = [{"idx": i, "text": f"第{i}句话很短，也够用了。", "est_sec": 3.0} for i in range(10)]
    script_data = {"title": "t", "hook": "h", "segments": segments, "total_sec": 30.0, "tts_rate": 4.2}
    result = storyboard.run(make_ctx(Stage.STORYBOARD, script_data, settings))
    assert len(result.data["shots"]) <= 10


def test_storyboard_rejects_bad_upstream(settings):
    with pytest.raises(AppError) as exc:
        storyboard.run(make_ctx(Stage.STORYBOARD, {"shots": []}, settings))
    assert exc.value.code is ErrCode.E_VALIDATION
