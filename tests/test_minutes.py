"""本地纪要整理（无大模型兜底）的行为约束。

这里的用例都来自真实的坏输出：
- 模型返回 "The request was rejected because it was considered high risk"
- 老版本把这句话原样写进了纪要文件
所以重点断言「不要把噪声当结论/待办」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import minutes


def _recs(texts: list[str], start: datetime) -> list[dict]:
    return [{"ts": (start + timedelta(seconds=i * 20)).timestamp(), "text": t}
            for i, t in enumerate(texts)]


START = datetime(2026, 11, 20, 10, 0)
END = START + timedelta(minutes=16)


def test_empty_transcript_still_returns_structure():
    md = minutes.local_meeting([], START, END)
    for head in ("## 会议摘要", "## 关键结论", "## 行动项", "## 待确认 / 风险"):
        assert head in md


def test_backchannels_are_dropped():
    recs = _recs(["嗯", "好的", "对对对", "OK", "行吧"], START)
    md = minutes.local_meeting(recs, START, END)
    assert "嗯" not in md
    assert "好的" not in md
    assert "没有识别到有效语音内容" in md


def test_negated_decision_is_not_a_conclusion():
    """「不确定」里含「确定」，但它是没定，不是结论。"""
    recs = _recs([
        "这个方案到底怎么定我还不确定，需要再问一下客户那边的意见",
        "那我们就按邮箱方案来做，这个已经跟客户确认过了",
    ], START)
    md = minutes.local_meeting(recs, START, END)
    conclusion = md.split("## 关键结论")[1].split("## 行动项")[0]
    assert "还不确定" not in conclusion
    assert "按邮箱方案" in conclusion


def test_action_needs_intent_not_past_narration():
    """「我发到群里了」是复述已发生的事，不能进待办表。"""
    recs = _recs([
        "我发到群里那个文件是昨天下午的版本",
        "我稍后去测试环境建一个带权限的邮箱",
    ], START)
    md = minutes.local_meeting(recs, START, END)
    table = md.split("## 行动项")[1].split("## 待确认")[0]
    assert "测试环境" in table
    assert "昨天下午" not in table


def test_questions_do_not_become_risks():
    recs = _recs([
        "那你现在遇到啥问题了",
        "如果保持登录这块做不了，后面联调会有风险",
    ], START)
    md = minutes.local_meeting(recs, START, END)
    risks = md.split("## 待确认 / 风险")[1]
    assert "会有风险" in risks
    assert "遇到啥问题" not in risks


def test_echo_lines_deduped():
    line = "我们下周一把上线窗口定在周二上午，提前一天封版"
    recs = _recs([line, line, line], START)
    md = minutes.local_meeting(recs, START, END)
    assert md.count("封版") <= 2      # 摘要 + 讨论要点各一次，不能三段全留


def test_action_table_is_markdown_parseable():
    recs = _recs(["我稍后去把接口文档补充一下，发给对方确认"], START)
    md = minutes.local_meeting(recs, START, END)
    table = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert table and all(ln.count("|") == 5 for ln in table)   # 4 列


def test_local_daily_merges_meeting_digests():
    recs = _recs(["今天主要在改简历和看岗位"], START)
    md = minutes.local_daily(recs, "2026-11-20",
                             digests=[{"title": "上线排期会", "digest": "## 关键结论\n- 下周一上线"}])
    assert "上线排期会" in md
    assert "下周一上线" in md
