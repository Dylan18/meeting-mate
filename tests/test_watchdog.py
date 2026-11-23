"""音频看门狗测试。

现场故障：VAD 的噪声底被顶到 0.05、阈值 0.13，采集和混音线程都还活着，
但一整天一个片段都不出，界面上全是绿的。看门狗负责把这类"静默失效"
要么自愈，要么明确说出来。
"""
from __future__ import annotations

import pytest

from app.assistant import Assistant


class _FakeSeg:
    def __init__(self):
        self.recalibrated = 0

    def recalibrate(self) -> None:
        self.recalibrated += 1


class _FakeHub:
    def __init__(self, **health):
        self.base = health
        self.segmenter = _FakeSeg()
        self.stopped = 0
        self.started = 0

    def health(self) -> dict:
        h = {
            "running": True,
            "tick": 1000,
            "tick_age": 0.1,
            "uptime": 600.0,
            "mic_level": 0.02,
            "sys_level": 0.0,
            "captured": {"麦克风": 6000},
            "mic_fail": 0,
            "vad": {"floor": 0.006, "threshold": 0.009, "active": False,
                    "buffered_sec": 0.0, "segments": 12, "dropped_noise": 0,
                    "peak_60s": 0.05, "noisy": False, "since_segment": 5.0},
        }
        h.update(self.base)
        return h

    def stop(self) -> None:
        self.stopped += 1

    def start(self, **kw) -> bool:
        self.started += 1
        return True


@pytest.fixture()
def asst(monkeypatch):
    a = Assistant(lambda k, d: None)
    a.listening = True

    def _fake_start() -> bool:
        # 自愈重启会真的去开设备，测试里只记录调用
        a.listening = True
        return a.hub.start()

    monkeypatch.setattr(a, "start_listening", _fake_start)
    yield a
    try:
        a.transcriber.stop(drain_timeout=1.0)
    except Exception:
        pass


def _vad(**kw):
    base = {"floor": 0.006, "threshold": 0.009, "active": False,
            "buffered_sec": 0.0, "segments": 12, "dropped_noise": 0,
            "peak_60s": 0.05, "noisy": False, "since_segment": 5.0}
    base.update(kw)
    return base


def test_healthy_state_sets_no_hint(asst):
    asst.hub = _FakeHub()
    asst._check_audio()
    assert asst.audio_hint is None


def test_stalled_mixer_triggers_restart(asst):
    asst.hub = _FakeHub(tick_age=9.0)
    asst._check_audio()
    assert asst.hub.stopped == 1
    assert asst.hub.started == 1
    assert "重启" in (asst.audio_hint or "")


def test_silent_device_is_reported(asst):
    asst.hub = _FakeHub(mic_level=0.0, sys_level=0.0, uptime=90.0)
    asst._check_audio()
    assert "麦克风没有声音" in (asst.audio_hint or "")


def test_no_segment_with_loud_audio_recalibrates(asst):
    """有声音、没片段：先自己重算噪声底，同时把原因说清楚。"""
    asst.hub = _FakeHub(vad=_vad(peak_60s=0.12, threshold=0.05, since_segment=400.0),
                        uptime=500.0)
    asst._check_audio()
    assert asst.hub.segmenter.recalibrated == 1
    assert "没有识别到内容" in (asst.audio_hint or "")


def test_noisy_environment_warns_before_failing(asst):
    asst.hub = _FakeHub(vad=_vad(floor=0.035, threshold=0.05, noisy=True), uptime=120.0)
    asst._check_audio()
    assert "噪声偏大" in (asst.audio_hint or "")


def test_hint_is_cleared_when_text_arrives(asst, monkeypatch):
    asst.hub = _FakeHub(mic_level=0.0, sys_level=0.0, uptime=90.0)
    asst._check_audio()
    assert asst.audio_hint
    events: list[tuple[str, object]] = []
    asst.on_event = lambda k, d: events.append((k, d))
    monkeypatch.setattr("app.assistant.store.append_segment", lambda rec: None)
    asst._on_text({"ts": 1.0, "text": "有内容了"})
    assert asst.audio_hint is None
    assert ("hint", "") in events


def test_hint_not_spammed(asst):
    """同一句提示 3 分钟内不重复弹，避免 toast 刷屏。"""
    asst.hub = _FakeHub(vad=_vad(floor=0.04, noisy=True), uptime=300.0)
    events: list[str] = []
    asst.on_event = lambda k, d: events.append(k) if k == "hint" else None
    asst._check_audio()
    asst._check_audio()
    assert events.count("hint") == 1
