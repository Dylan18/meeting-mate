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


# ---------------------------------------------------------------- 噪声底自锁回归
# 现场故障：下午空调开大，噪声底被顶到 0.05，判定阈值 0.13，采集线程还在跑、
# 电平条还在跳，但一整天一个片段都不出，界面也没有任何报错。
def _noise_blocks(sec: float, rms: float, seed: int = 5) -> list[np.ndarray]:
    return _blocks(_noise(sec, rms, seed=seed))


def _voice(sec: float, rms: float, seed: int = 1) -> np.ndarray:
    """按指定 RMS 归一化后的"人声"（保留音节起伏）。"""
    x = _speech(sec, seed=seed)
    return (x / float(np.sqrt(np.mean(x ** 2))) * rms).astype(np.float32)


def test_noisy_room_still_detects_speech():
    """底噪 0.03（空调开大）+ 正常说话 0.06，必须还能切出片段。"""
    parts = []
    for i in range(5):
        parts.append(_noise(20, 0.03, seed=i))
        parts.append(_voice(3, 0.06, seed=20 + i))
    segs, seg = _run(_blocks(np.concatenate(parts).astype(np.float32)))
    assert len(segs) >= 4
    assert seg.noise_floor > 0.02          # 底噪跟到了环境水平
    assert seg.current_threshold <= 0.05   # 但阈值有上限，不能把说话挡住


def test_quiet_then_noisy_does_not_lock_out():
    """先安静 20s 再变吵（底噪 0.04）：噪声底可以跟着抬，但不能抬到听不见人声。"""
    parts = [_noise(20, 0.006)]
    for i in range(5):
        parts.append(_noise(20, 0.04, seed=30 + i))
        parts.append(_voice(3, 0.07, seed=40 + i))
    segs, seg = _run(_blocks(np.concatenate(parts).astype(np.float32)))
    assert len(segs) >= 3
    assert seg.current_threshold <= 0.051


def test_low_gain_speech_detected():
    """麦克风增益偏低（说话只有 0.015，底噪 0.008）也要能识别。"""
    parts = []
    for i in range(5):
        parts.append(_noise(20, 0.008, seed=50 + i))
        parts.append(_voice(3, 0.015, seed=60 + i))
    segs, _ = _run(_blocks(np.concatenate(parts).astype(np.float32)))
    assert len(segs) >= 3


def test_threshold_has_absolute_ceiling():
    """再吵的环境，阈值也不能超过 0.05（否则正常说话的 0.06 就被判成静音）。"""
    seg = VADSegmenter(lambda a: None)
    seg.noise_floor = 0.2
    assert seg.current_threshold <= 0.0501


def test_stats_exposes_progress_for_watchdog():
    audio = np.concatenate([np.zeros(16000 * 2, np.float32), _speech(1.5),
                            np.zeros(16000 * 2, np.float32)])
    segs, seg = _run(_blocks(audio))
    st = seg.stats()
    assert len(segs) == 1
    assert st["segments"] == 1
    assert st["since_segment"] >= 0
    assert st["threshold"] >= 0.006


def test_long_run_never_goes_deaf():
    """长时间运行的回归：安静 → 变吵 → 又变安静，每个阶段都必须还能出片段。

    这是本次现场故障的完整复现路径：早上安静能用，下午环境变吵，此后一整天
    零片段。噪声底只升不降的那种实现过不了这条。
    """
    rng = np.random.default_rng(99)
    phases = [("安静", 0.005, 8), ("变吵", 0.035, 8), ("又安静", 0.004, 8)]
    out: list[float] = []
    seg = VADSegmenter(lambda a: out.append(len(a) / 16000))
    per_phase: list[int] = []

    for name, bg, bursts in phases:
        before = len(out)
        for i in range(bursts):
            noise = rng.normal(0, bg, size=16000 * 12).astype(np.float32)
            voice = _voice(3, max(0.06, bg * 2.0), seed=200 + i)
            gap = rng.normal(0, bg, size=16000 * 3).astype(np.float32)
            sig = np.concatenate([noise, voice, gap])
            for b in _blocks(sig):
                seg.feed(b)
        per_phase.append(len(out) - before)
        print(f"  {name}: 底噪 {seg.noise_floor:.4f} 阈值 {seg.current_threshold:.4f} "
              f"片段 {per_phase[-1]}")

    assert all(n >= 5 for n in per_phase), f"某个阶段完全没片段: {per_phase}"
    assert seg.current_threshold <= 0.0501


def _wobbly_ambient(sec: float, seed: int = 3) -> list[np.ndarray]:
    """起伏的现场噪声：每块电平在 0.006~0.02 之间随机（人走动、椅子、键盘、远处说话）。

    纯白噪声的帧间变异系数只有 0.02，会被「稳态噪声」兜底直接吃掉，反而掩盖问题；
    真实办公室噪声的变异系数在 0.3 上下，既过不了稳态判据，又永远高于被压低的阈值。
    """
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(int(sec * 10)):
        rms = rng.uniform(0.006, 0.02)
        out.append(rng.normal(0, rms, size=BLOCK).astype(np.float32))
    return out


def test_calibration_ignores_digital_silence_window():
    """启动瞬间采集流回全零，不能拿它当噪声底。

    现场复现：每次点开始监听，前 2 秒麦克风是数字静音，噪声底被算成 0.0008，
    阈值压到下限 0.006；随后环境噪声 0.01 全程高于阈值，VAD 再也分不出停顿。
    """
    seg = VADSegmenter(lambda a: None, calib_sec=2.0)
    for b in [np.zeros(BLOCK, dtype=np.float32)] * 20:
        seg.feed(b)
    assert seg.noise_floor <= 0.002      # 第一窗不可信，还没定下来
    for b in _blocks(_noise(3.0, 0.012)):
        seg.feed(b)
    print(f"  数字静音后校准: 底噪 {seg.noise_floor:.4f} 阈值 {seg.current_threshold:.4f}")
    assert seg.noise_floor > 0.006, "把数字静音当成噪声底了"


def test_forced_flush_triggers_recalibration():
    """连续被 20 秒上限强切 → 必须重新校准，不能一直吐 20 秒的粘连片段。

    修复前：噪声底卡在 0.0008，每 20 秒强切一段，一整天都是大段粘连文字。
    """
    out: list[np.ndarray] = []
    seg = VADSegmenter(lambda a: out.append(a), calib_sec=1.0)
    for b in _wobbly_ambient(80):
        seg.feed(b)
    print(f"  强切后: 底噪 {seg.noise_floor:.4f} 阈值 {seg.current_threshold:.4f} "
          f"片段 {len(out)} 长段 {sum(1 for a in out if a.size >= BLOCK * 195)}")
    assert seg.noise_floor > 0.005, "噪声底卡死在最低档，没有自我修正"
    assert seg.current_threshold > 0.006, "阈值仍在下限，永远分不出停顿"
