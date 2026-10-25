"""状态机与幂等键：纯函数，必须 100% 可预测。"""

from __future__ import annotations

from app.core.idem import idem_key, inputs_hash, short_hash
from app.core.state_machine import (
    AGENT_STAGES,
    STAGE_ORDER,
    Stage,
    TaskStatus,
    is_terminal,
    next_stage,
    prev_stage,
)


def test_stage_order_is_seven():
    assert [s.value for s in STAGE_ORDER] == [
        "RESEARCH", "SCRIPT", "STORYBOARD", "ASSETS", "VOICE", "RENDER", "QC"
    ]


def test_next_prev():
    assert next_stage(Stage.RESEARCH) is Stage.SCRIPT
    assert next_stage(Stage.QC) is None
    assert prev_stage(Stage.RESEARCH) is None
    assert prev_stage(Stage.QC) is Stage.RENDER


def test_three_agent_stages():
    assert AGENT_STAGES == {Stage.RESEARCH, Stage.ASSETS, Stage.QC}


def test_terminal():
    assert is_terminal("DONE") and is_terminal("PARTIAL")
    assert not is_terminal("PENDING") and not is_terminal(TaskStatus.RUNNING.value)


class TestIdemKey:
    def test_stable(self):
        up = {"a": 1, "b": [1, 2, 3]}
        assert idem_key("t_1", Stage.SCRIPT, up) == idem_key("t_1", Stage.SCRIPT, up)

    def test_volatile_fields_ignored(self):
        a = {"angle": "x", "cost_cents": 3, "created_at": "2026-01-01"}
        b = {"angle": "x", "cost_cents": 99, "created_at": "2026-12-31"}
        assert inputs_hash(a) == inputs_hash(b)

    def test_content_change_changes_key(self):
        assert idem_key("t_1", Stage.SCRIPT, {"angle": "A"}) != idem_key("t_1", Stage.SCRIPT, {"angle": "B"})

    def test_stage_and_task_isolated(self):
        assert idem_key("t_1", Stage.SCRIPT, {}) != idem_key("t_1", Stage.QC, {})
        assert idem_key("t_1", Stage.SCRIPT, {}) != idem_key("t_2", Stage.SCRIPT, {})

    def test_empty_upstream_stable(self):
        assert inputs_hash({}) == inputs_hash(None) == short_hash("__root__")
