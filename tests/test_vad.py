"""VAD 行为测试。

重点是 P0-1：恒定噪声下不能永久判为"有人说话"。这条用例直接复现了审阅时的实测场景
（30s、RMS 0.01 的白噪声），修复前会吐出 1 段 20 秒的假语音。
"""
from __future__ import annotations

import numpy as np

from app.audio import BLOCK, VADSegmenter


def _run(blocks, flush: bool = True, **kw):
    out: list[np.ndarray] = []
    seg = VADSegmenter(lambda a: out.append(a), **kw)
    for b in blocks:
        seg.feed(b)
    if flush:
        seg.flush_pending()
    return out, seg


def _blocks(x: np.ndarray) -> list[np.ndarray]:
    return [x[i:i + BLOCK] for i in range(0, x.size, BLOCK) if x[i:i + BLOCK].size == BLOCK]


def _noise(sec: float, rms: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0, rms, size=int(16000 * sec)).astype(np.float32)


def _speech(sec: float, seed: int = 11) -> np.ndarray:
    """带 4Hz 音节包络的噪声，比纯白噪声更接近人声的能量起伏。"""
    rng = np.random.default_rng(seed)
    t = np.arange(int(16000 * sec)) / 16000
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    return (rng.normal(0, 0.12, size=t.size) * env).astype(np.float32)


def test_constant_noise_produces_no_segment():
    """办公室空调/风扇级别的稳态底噪，不能产出任何片段。"""
    segs, seg = _run(_blocks(_noise(30, 0.01)))
    assert segs == []
    assert seg.noise_floor > 0.008      # 噪声底被正确抬到环境水平
    assert seg.dropped_noise == 0       # 校准就直接定对了，不需要靠兜底丢弃


def test_silence_produces_no_segment():
    segs, _ = _run([np.zeros(BLOCK, dtype=np.float32)] * 100)
    assert segs == []


def test_single_utterance_detected():
    audio = np.concatenate([np.zeros(16000 * 2, np.float32), _speech(1.2),
                            np.zeros(16000 * 2, np.float32)])
    segs, _ = _run(_blocks(audio))
    assert len(segs) == 1
    assert 1.0 <= len(segs[0]) / 16000 <= 2.0


def test_long_speech_split_by_max_len():
    segs, _ = _run(_blocks(_speech(30)), max_len=20.0)
    assert len(segs) == 2
    assert max(len(s) / 16000 for s in segs) <= 20.01


def test_calibration_not_fooled_by_starting_mid_speech():
    """用户点开始监听时正在说话，校准不能把阈值顶到天上去。"""
    segs, _ = _run(_blocks(_speech(8)))
    assert len(segs) >= 1
    assert segs[0].size > 16000        # 至少抓到 1 秒


def test_noise_that_starts_later_is_absorbed():
    """开始安静、中途变吵：稳态噪声兜底要把它吃掉，而不是当成一场长会。"""
    audio = np.concatenate([np.zeros(16000 * 3, np.float32), _noise(20, 0.02, seed=3)])
    segs, seg = _run(_blocks(audio))
    assert segs == []
    assert seg.dropped_noise >= 1


def test_quiet_room_keeps_sensitivity():
    """底噪 0.005（低于默认阈值）时，正常说话仍要能识别。"""
    audio = np.concatenate([_noise(3, 0.005), _speech(1.5), _noise(3, 0.005)])
    segs, _ = _run(_blocks(audio))
    assert len(segs) == 1
