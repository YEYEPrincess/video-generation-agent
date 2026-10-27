"""译制流水线的纯函数单测：不联网、不跑模型。"""

from __future__ import annotations

from app.dub.media import place_clips
from app.dub.qc import check
from app.dub.subs import _ts, build_srt
from app.dub.types import Clip, Segment


def _seg(i: int, start: float, end: float, text: str = "中文", en: str = "english") -> Segment:
    return Segment(idx=i, start=start, end=end, text=text, translated=en)


def test_ts_format():
    assert _ts(0) == "00:00:00,000"
    assert _ts(3661.5) == "01:01:01,500"


def test_place_clips_keeps_order_and_no_overlap():
    # 三句紧挨着，第二句配音超长，应该把第三句往后推
    segments = [_seg(0, 0.0, 2.0), _seg(1, 2.0, 4.0), _seg(2, 4.0, 6.0)]
    clips = [
        Clip(idx=0, path=__import__("pathlib").Path("a.mp3"), duration=1.8),
        Clip(idx=1, path=__import__("pathlib").Path("b.mp3"), duration=2.6),  # 超出 slot
        Clip(idx=2, path=__import__("pathlib").Path("c.mp3"), duration=1.5),
    ]
    placed, drift = place_clips(segments, clips)

    assert [c.placed_at for c in placed] == sorted(c.placed_at for c in placed)
    for prev, cur in zip(placed, placed[1:]):
        assert cur.placed_at >= prev.placed_at + prev.duration, "配音不能重叠"
    assert drift > 0, "第二句超长必然产生漂移"
    assert placed[2].placed_at > segments[2].start


def test_srt_uses_placed_time_not_source_time():
    segments = [_seg(0, 0.0, 2.0), _seg(1, 2.0, 4.0)]
    clips = [
        Clip(idx=0, path=__import__("pathlib").Path("a.mp3"), duration=1.5, placed_at=0.0),
        Clip(idx=1, path=__import__("pathlib").Path("b.mp3"), duration=1.5, placed_at=3.0),
    ]
    out = __import__("pathlib").Path("dev_out/_test.srt")
    build_srt(segments, clips, out)
    body = out.read_text(encoding="utf-8")

    assert "00:00:03,000 --> 00:00:04,500" in body, "字幕必须跟随配音落点"
    out.unlink(missing_ok=True)


def test_qc_flags_over_speed():
    segments = [_seg(0, 0.0, 2.0), _seg(1, 2.0, 4.0)]
    clips = [
        Clip(idx=0, path=__import__("pathlib").Path("a.mp3"), duration=1.8, placed_at=0.0),
        Clip(idx=1, path=__import__("pathlib").Path("b.mp3"), duration=1.8, placed_at=2.0, speed=1.6),
    ]
    result = check(segments, clips, src_duration=4.0, out_duration=4.1, srt_count=2)
    assert result["checks"]["speed_reasonable"] is False
    assert result["checks"]["duration_match"] is True
    assert result["passed"] is False


def test_qc_passes_clean_case():
    segments = [_seg(0, 0.0, 2.0), _seg(1, 2.0, 4.0)]
    clips = [
        Clip(idx=0, path=__import__("pathlib").Path("a.mp3"), duration=1.9, placed_at=0.0, speed=1.1),
        Clip(idx=1, path=__import__("pathlib").Path("b.mp3"), duration=1.9, placed_at=2.0, speed=1.1),
    ]
    result = check(segments, clips, src_duration=4.0, out_duration=4.0, srt_count=2)
    assert result["passed"] is True
