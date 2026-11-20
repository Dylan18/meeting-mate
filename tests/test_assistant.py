"""编排层测试：任务锁原子性、退出排空队列、模型未就绪暂存。"""
from __future__ import annotations

import numpy as np
import pytest

from app.asr import Transcriber
from app.assistant import Assistant


@pytest.fixture()
def asst():
    a = Assistant(lambda k, d: None)
    yield a
    try:
        a.transcriber.stop(drain_timeout=1.0)
    except Exception:
        pass


def test_job_lock_is_atomic(asst):
    """P0-2：第二次占位必须失败，不能出现两个并发的 LLM 任务。"""
    assert asst._try_begin("任务一") is True
    assert asst._try_begin("任务二") is False
    assert asst.busy is True
    asst._end_job()
    assert asst.busy is False
    assert asst._try_begin("任务三") is True
    asst._end_job()


def test_generate_daily_rejected_when_busy(asst):
    asst._try_begin("正在生成会议纪要…")
    assert asst.generate_daily() is False
    asst._end_job()


def test_double_end_job_does_not_raise(asst):
    asst._try_begin("x")
    asst._end_job()
    asst._end_job()          # 重复释放只记日志，不能炸掉调用方


# ---------------------------------------------------------------- 转写队列
class _FakeStream:
    def __init__(self, text):
        self.result = type("R", (), {"text": text})()

    def accept_waveform(self, sr, x):
        pass


class _FakeRec:
    def __init__(self):
        self.n = 0

    def create_stream(self):
        self.n += 1
        return _FakeStream(f"识别内容第{self.n}条")

    def decode_stream(self, stream):
        pass


def _audio(n=1600):
    return np.ones(n, dtype=np.float32) * 0.1


def test_stop_drains_pending_queue():
    """P0-3：退出时队列里剩下的音频必须转写完，不能直接丢。"""
    got: list[dict] = []
    t = Transcriber(lambda rec: got.append(rec))
    t._rec = _FakeRec()
    for _ in range(5):
        t.submit(_audio(), {"dur": 0.1, "scope": "daily"})
    assert t.stop(drain_timeout=5) is True
    assert len(got) == 5


def test_submit_after_stop_is_dropped():
    t = Transcriber(lambda rec: None)
    t._rec = _FakeRec()
    t.stop(drain_timeout=2)
    t.submit(_audio(), {"dur": 0.1})
    assert t.dropped == 1


def test_audio_buffered_until_model_ready():
    """P0-4：模型没就绪时不能直接丢音频，要暂存并在就绪后补转。"""
    got: list[dict] = []
    t = Transcriber(lambda rec: got.append(rec))
    for _ in range(3):
        t.submit(_audio(), {"dur": 0.1})
    assert t.pending() == 3
    assert got == []

    t._rec = _FakeRec()
    t._flush_backlog()
    assert t.stop(drain_timeout=5) is True
    assert len(got) == 3
