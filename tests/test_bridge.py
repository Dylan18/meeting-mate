"""桥接层测试：事件增量与 gap 判定（不写配置、不起真实音频）。"""
from __future__ import annotations

import pytest

from app.ui.bridge import Bridge, _within


@pytest.fixture()
def bridge():
    b = Bridge(start_scheduler=False)
    yield b
    b._closing = True


def test_level_events_are_coalesced(bridge):
    for v in (0.1, 0.2, 0.3):
        bridge._push("level", (v, v))
    levels = [e for e in bridge.events if e["kind"] == "level"]
    assert len(levels) == 1
    assert levels[0]["data"] == [0.3, 0.3]


def test_poll_does_not_report_gap_after_seq_jump(bridge):
    """level 合并会让 seq 跳号，不能因此误报"记录已滚出缓存"。"""
    bridge._push("level", (0.1, 0.1))
    bridge._push("level", (0.2, 0.2))
    bridge._push("text", {"ts": 1, "text": "x"})
    r = bridge.poll(bridge._seq - 1)
    assert r["gap"] is False
    assert [e["kind"] for e in r["events"]] == ["text"]


def test_poll_reports_gap_when_events_actually_dropped(bridge):
    bridge.events = [{"seq": 5000, "kind": "text", "data": {}}]
    bridge._dropped_below = 4999
    assert bridge.poll(10)["gap"] is True
    assert bridge.poll(4999)["gap"] is False


def test_poll_from_zero_never_gaps(bridge):
    bridge._push("notice", "hi")
    assert bridge.poll(0)["gap"] is False


def test_within_helper():
    from pathlib import Path
    root = Path.cwd()
    assert _within(root / "a" / "b.md", root) is True
    assert _within(Path("C:/Windows/win.ini"), root) is False
