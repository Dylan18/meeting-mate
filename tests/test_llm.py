"""LLM 层测试：分段、清洗、截断重试、5xx 重试、无 LLM 降级。

不联网：requests.post 用假响应替身，time.sleep 也被替换掉（否则重试要真等）。
"""
from __future__ import annotations

import pytest

from app import llm


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


def _ok(content, finish="stop"):
    return FakeResp(200, {"choices": [{"message": {"content": content}, "finish_reason": finish}]})


CFG = {"base_url": "http://fake/v1", "api_key": "sk-test", "model": "m", "timeout": 5}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)


def test_chunk_preserves_single_huge_line():
    text = "x" * (llm.CHUNK_CHARS + 500)
    chunks = llm._chunk(text)
    assert chunks == [text]          # 单行超长也必须完整保留，不能截断


def test_chunk_splits_on_line_boundaries():
    text = "\n".join(f"第{i}行" + "啊" * 100 for i in range(200))
    chunks = llm._chunk(text)
    assert len(chunks) > 1
    assert all(len(c) <= llm.CHUNK_CHARS + 200 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_clean_strips_code_fence():
    assert llm._clean("```markdown\n# 标题\n正文\n```") == "# 标题\n正文"
    assert llm._clean("  plain  ") == "plain"


def test_chat_retries_with_bigger_budget_on_length(monkeypatch):
    calls = []

    def fake_post(url, **kw):
        budget = __import__("json").loads(kw["data"])["max_tokens"]
        calls.append(budget)
        if len(calls) == 1:
            return _ok("前半段", finish="length")
        return _ok("完整内容", finish="stop")

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.chat([{"role": "user", "content": "x"}], max_tokens=100, cfg=CFG) == "完整内容"
    assert len(calls) == 2
    assert calls[1] > calls[0]


def test_chat_marks_partial_when_still_truncated(monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _ok("只有一半", finish="length"))
    out = llm.chat([{"role": "user", "content": "x"}], max_tokens=100, cfg=CFG)
    assert "可能不完整" in out
    assert "只有一半" in out


def test_chat_retries_on_503(monkeypatch):
    seq = [FakeResp(503, text="busy"), _ok("好了")]

    def fake_post(*a, **k):
        return seq.pop(0)

    monkeypatch.setattr(llm.requests, "post", fake_post)
    assert llm.chat([{"role": "user", "content": "x"}], cfg=CFG) == "好了"


def test_chat_raises_on_401(monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResp(401, text="bad key"))
    with pytest.raises(llm.LLMError) as ei:
        llm.chat([{"role": "user", "content": "x"}], cfg=CFG)
    assert "401" in str(ei.value)


def test_chat_requires_key():
    with pytest.raises(llm.LLMError):
        llm.chat([{"role": "user", "content": "x"}], cfg={"base_url": "http://x/v1", "model": "m"})


def test_reduce_keeps_order(monkeypatch):
    """并发提炼必须按下标保序，否则拼回去的要点是乱的。"""
    def fake_chat(messages, max_tokens=0, temperature=0, cfg=None):
        return messages[-1]["content"][-3:]

    monkeypatch.setattr(llm, "chat", fake_chat)
    out = llm._reduce(["AAA-1", "BBB-2", "CCC-3"], "hint")
    assert out.index("-1") < out.index("-2") < out.index("-3")


def test_fallback_daily_without_llm():
    segs = [{"ts": 1700000000, "text": "开会讨论排期"}, {"ts": 1700000100, "text": ""}]
    body = llm.fallback_daily(segs, "2026-11-20")
    assert "开会讨论排期" in body
    assert "共 1 条" in body


def test_fallback_daily_includes_meeting_digests():
    body = llm.fallback_daily([], "2026-11-20", digests=[
        {"title": "排期会", "digest": "## 关键结论\n下周一上线"}
    ])
    assert "排期会" in body and "下周一上线" in body
