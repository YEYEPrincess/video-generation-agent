"""契约测试：字段约束是整套系统的地基，先钉死它。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.contracts import (
    AssetShot,
    Assets,
    Check,
    QCReport,
    ResearchBrief,
    Script,
    Segment,
    Shot,
    Storyboard,
    Video,
)


def _script(**over) -> dict:
    base = {
        "title": "老动画为什么又火了",
        "hook": "你有没有发现，最近弹幕里全是考古的",
        "segments": [
            {"idx": 0, "text": "第一段讲现象，大概二十个字左右。", "est_sec": 5.0},
            {"idx": 1, "text": "第二段讲原因，也差不多这么长。", "est_sec": 5.0},
            {"idx": 2, "text": "第三段收尾，给出一个反问作为钩子。", "est_sec": 5.0},
        ],
        "total_sec": 15.0,
    }
    base.update(over)
    return base


class TestResearchBrief:
    def test_valid(self):
        brief = ResearchBrief(
            angle="从怀旧经济角度切，讲平台与观众的双向选择",
            hooks=["三连了十年的老番", "弹幕为什么突然变多", "谁在推这些老片"],
            refs=[{"title": "老番考古", "ref": "BV1xx411c7mD", "why": "同类选题数据参考"}],
            keywords=["老番", "考古", "怀旧", "弹幕", "重看"],
        )
        assert len(brief.refs) == 1

    def test_hooks_must_be_exactly_three(self):
        with pytest.raises(ValidationError):
            ResearchBrief(
                angle="从怀旧经济角度切，讲平台与观众的双向选择",
                hooks=["只有两个", "钩子"],
                refs=[{"title": "x", "ref": "BV1", "why": "y"}],
                keywords=["a", "b", "c"],
            )

    def test_angle_length(self):
        with pytest.raises(ValidationError):
            ResearchBrief(angle="太短", hooks=["a", "b", "c"],
                          refs=[{"title": "x", "ref": "BV1", "why": "y"}],
                          keywords=["a", "b", "c"])


class TestScript:
    def test_valid_roundtrip(self):
        s = Script(**_script())
        assert s.total_sec == 15.0

    def test_title_too_long(self):
        with pytest.raises(ValidationError):
            Script(**_script(title="啊" * 31))

    def test_title_exactly_30_ok(self):
        assert len(Script(**_script(title="啊" * 30)).title) == 30

    def test_segments_must_be_at_least_three(self):
        with pytest.raises(ValidationError):
            Script(**_script(segments=[{"idx": 0, "text": "只有一段。", "est_sec": 3.0}]))


class TestStoryboard:
    def test_segment_idx_must_exist(self):
        sb = Storyboard(shots=[
            {"idx": i, "segment_idx": i, "narration": f"第{i}镜的旁白内容在这里。",
             "visual_prompt": "老式电视机，暖光，特写", "est_sec": 5.0}
            for i in range(3)
        ])
        assert len(sb.shots) == 3

    def test_too_many_shots(self):
        with pytest.raises(ValidationError):
            Storyboard(shots=[
                {"idx": i, "segment_idx": 0, "narration": "旁白内容足够长。",
                 "visual_prompt": "构图", "est_sec": 3.0}
                for i in range(11)
            ])


class TestDownstream:
    def test_assets_and_qc(self):
        a = Assets(shots=[AssetShot(idx=0, kind="image", path="a.png", width=1920, height=1080)])
        assert a.shots[0].degraded is False

        qc = QCReport(checks=[Check(name="duration", passed=True)], score=100)
        assert qc.decided == "DONE"

    def test_video_defaults(self):
        v = Video(path="final.mp4")
        assert v.duration_sec == 0.0 and v.has_audio is False


def test_stage_contracts_cover_all_stages():
    from app.contracts import STAGE_CONTRACTS
    from app.core.state_machine import STAGE_ORDER

    assert set(STAGE_CONTRACTS) == {s.value for s in STAGE_ORDER}
