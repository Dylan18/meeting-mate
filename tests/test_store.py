"""存储层测试：追加/读取、坏行跳过、报告 header、会议摘要抽取。"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import store


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    (tmp_path / "sessions").mkdir()
    (tmp_path / "reports").mkdir()
    (tmp_path / "meetings").mkdir()
    monkeypatch.setattr(store, "SESSION_DIR", tmp_path / "sessions")
    monkeypatch.setattr(store, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(store, "MEETING_DIR", tmp_path / "meetings")
    return tmp_path


def test_append_and_load_roundtrip():
    store.append_segment({"ts": 1.0, "text": "第一条", "scope": "daily"})
    store.append_segment({"ts": 2.0, "text": "第二条", "scope": "meeting"})
    recs = store.load_segments()
    assert [r["text"] for r in recs] == ["第一条", "第二条"]


def test_bad_line_is_skipped():
    p = store.SESSION_DIR / f"{store.today_key()}.jsonl"
    p.write_text('{"ts":1,"text":"好"}\n{坏行\n{"ts":2,"text":"也好"}\n', encoding="utf-8")
    recs = store.load_segments()
    assert len(recs) == 2


def test_render_puts_time_prefix():
    txt = store.render([{"ts": 1700000000, "text": "hello"}])
    assert txt.startswith("[")
    assert txt.endswith("hello")


def test_write_daily_report_header(tmp_dirs):
    path = store.write_daily_report("2026-11-20", "## 今日概览\n干活", {"segments": 7})
    text = path.read_text(encoding="utf-8")
    assert path.name == "日报_2026-11-20.md"
    assert "# 2026-11-20 工作日报" in text
    assert "语音片段 7 条" in text
    assert "## 今日概览" in text


def test_write_meeting_report_strips_duplicate_title(tmp_dirs):
    start = datetime(2026, 11, 20, 14, 5)
    path = store.write_meeting_report(start, "排期会", "# 排期会\n\n## 会议摘要\n内容",
                                      {"end": datetime(2026, 11, 20, 15, 0),
                                       "duration": "55 分钟", "segments": 12})
    text = path.read_text(encoding="utf-8")
    assert path.name.startswith("会议_20261120_1405")
    assert text.count("# 排期会") == 1        # 标题只出现一次
    assert "55 分钟" in text


def test_meeting_titles_and_digests(tmp_dirs):
    start = datetime(2026, 11, 20, 9, 0)
    body = ("# 需求评审\n\n## 会议摘要\n过长的一段。\n\n"
            "## 关键结论\n本期只做三个需求。\n\n"
            "## 行动项\n| 事项 | 责任人 |\n|---|---|\n| 出原型 | 待定 |\n")
    store.write_meeting_report(start, "需求评审", body,
                               {"end": datetime(2026, 11, 20, 10, 0), "segments": 5})
    assert store.meeting_titles_for("2026-11-20") == ["需求评审"]
    digests = store.meeting_digests_for("2026-11-20")
    assert len(digests) == 1
    assert "本期只做三个需求" in digests[0]["digest"]
    assert "会议摘要" not in digests[0]["digest"]   # 摘要段不重复塞进日报


def test_has_daily_report(tmp_dirs):
    assert store.has_daily_report("2026-11-20") is False
    store.write_daily_report("2026-11-20", "x")
    assert store.has_daily_report("2026-11-20") is True


def test_segments_between_filters_by_time():
    store.append_segment({"ts": 1.2e9, "text": "a"})
    store.append_segment({"ts": 1.7e9, "text": "b"})
    got = store.segments_between(datetime.fromtimestamp(1e9), datetime.fromtimestamp(1.5e9))
    assert [r["text"] for r in got] == ["a"]


def test_meeting_sidecar_keeps_transcript(tmp_dirs):
    start = datetime(2026, 11, 20, 10, 24)
    segs = [{"ts": start.timestamp(), "text": "确认下周一上线"}]
    path = store.write_meeting_report(start, "上线会", "## 会议摘要\n聊上线",
                                      {"end": datetime(2026, 11, 20, 10, 40),
                                       "duration": "16 分钟", "segments": 1,
                                       "transcript": segs, "engine": "local",
                                       "note": "AI 被拦，本地整理"})
    data = store.load_meeting_data(path)
    assert data["engine"] == "local"
    assert data["segments"][0]["text"] == "确认下周一上线"
    assert store.meeting_data_path(path).parent.name == "meetings"


def test_meeting_sidecar_absent_when_no_transcript(tmp_dirs):
    path = store.write_meeting_report(datetime(2026, 11, 20, 9, 0), "", "## 会议摘要\nx")
    assert store.load_meeting_data(path) is None


def test_meeting_span_from_header(tmp_dirs):
    start = datetime(2026, 11, 20, 10, 24)
    path = store.write_meeting_report(start, "上线会", "## 会议摘要\nx",
                                      {"end": datetime(2026, 11, 20, 10, 40),
                                       "duration": "16 分钟", "segments": 3})
    got = store.meeting_span(path.read_text(encoding="utf-8"), path.stem)
    assert got[0] == start
    assert got[1] == datetime(2026, 11, 20, 10, 40)


def test_meeting_span_falls_back_to_filename():
    got = store.meeting_span("没有任何元信息", "会议_20261120_1024_上线会")
    assert got[0] == datetime(2026, 11, 20, 10, 24)


def test_meeting_report_can_overwrite_existing_path(tmp_dirs):
    """重新整理必须覆盖原文件，不能因为标题变了就多出一份副本。"""
    start = datetime(2026, 11, 20, 10, 24)
    p = tmp_dirs / "reports" / "会议_20261120_1024.md"
    p.write_text("坏内容", encoding="utf-8")
    got = store.write_meeting_report(start, "新标题", "## 会议摘要\n新内容",
                                     {"duration": "1 分钟"}, path=p)
    assert got == p
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# 新标题")
    assert "坏内容" not in text
