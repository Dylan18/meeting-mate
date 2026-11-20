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


# ---------------------------------------------------------------- 会议详情
def test_read_meeting_rejects_non_meeting_path(tmp_path, monkeypatch):
    import app.ui.bridge as bm
    monkeypatch.setattr(bm, "REPORT_DIR", tmp_path)
    p = tmp_path / "日报_2026-11-20.md"
    p.write_text("# x", encoding="utf-8")
    b = Bridge(start_scheduler=False)
    assert b.read_meeting(str(p))["ok"] is False


def test_read_meeting_flags_legacy_broken_minutes(tmp_path, monkeypatch):
    """早期版本把网关报错写进文件，详情页要明确告诉用户「这份无效」。"""
    import app.ui.bridge as bm
    from app import store
    monkeypatch.setattr(bm, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(store, "REPORT_DIR", tmp_path)
    p = tmp_path / "会议_20261120_1024.md"
    p.write_text(
        "# 会议纪要\n\n> 2026-11-20 10:24 — 10:40 ｜ 时长 16 分钟 ｜ 转写 3 段\n\n"
        "The request was rejected because it was considered high risk\n",
        encoding="utf-8",
    )
    r = Bridge(start_scheduler=False).read_meeting(str(p))
    assert r["ok"] is True
    assert "无效" in r["note"]


def test_read_meeting_returns_transcript_from_sidecar(tmp_path, monkeypatch):
    import app.ui.bridge as bm
    from app import store
    monkeypatch.setattr(bm, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(store, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(store, "MEETING_DIR", tmp_path / "meetings")
    (tmp_path / "meetings").mkdir()
    from datetime import datetime
    start = datetime(2026, 11, 20, 10, 24)
    p = store.write_meeting_report(start, "上线会", "## 会议摘要\n聊上线",
                                   {"end": datetime(2026, 11, 20, 10, 40),
                                    "duration": "16 分钟", "segments": 2,
                                    "engine": "local", "note": "AI 被拦",
                                    "transcript": [{"ts": 1.0, "text": "第一句"},
                                                   {"ts": 2.0, "text": "第二句"}]})
    r = Bridge(start_scheduler=False).read_meeting(str(p))
    assert r["title"] == "上线会"
    assert [x["text"] for x in r["transcript"]] == ["第一句", "第二句"]
    assert r["engine"] == "local" and r["note"] == "AI 被拦"
