"""音频采集：麦克风 + 系统回环双路并行采集、混音、VAD 自动切段。

设计要点：
- 每路一个采集线程，各自独立阻塞读取，避免互相拖累
- 混音线程按 100ms 固定节拍取最新帧，队列满则丢旧帧（保证实时性优先）
- 能量 VAD + 自适应噪声底，自动切成一句一句的语音片段送给转写引擎
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable

import numpy as np

try:
    import soundcard as sc
except Exception:  # pragma: no cover
    sc = None

SR = 16000          # 统一采样率
BLOCK_MS = 100
BLOCK = SR * BLOCK_MS // 1000   # 1600 frames


# ---------------------------------------------------------------- 设备枚举
def list_devices() -> dict:
    """返回 {'loopback': [...], 'mic': [...]}，元素为 (id, name)。"""
    if sc is None:
        return {"loopback": [], "mic": []}
    out = {"loopback": [], "mic": []}
    try:
        default_spk = sc.default_speaker()
        default_spk_id = default_spk.id if default_spk else None
    except Exception:
        default_spk_id = None

    try:
        for m in sc.all_microphones(include_loopback=True):
            if getattr(m, "isloopback", False):
                out["loopback"].append((m.id, m.name))
            else:
                out["mic"].append((m.id, m.name))
    except Exception:
        pass

    # 回环设备在 soundcard 里挂的是默认扬声器；若没枚举到，补一个
    if default_spk_id and not out["loopback"]:
        out["loopback"].append((default_spk_id, f"{default_spk.name} (回环)"))
    return out


def _resolve_loopback():
    if sc is None:
        return None
    try:
        for m in sc.all_microphones(include_loopback=True):
            if getattr(m, "isloopback", False):
                return m
    except Exception:
        pass
    try:
        spk = sc.default_speaker()
        if spk:
            return sc.get_microphone(spk.id, include_loopback=True)
    except Exception:
        pass
    return None


def _resolve_mic(device_id: str | None):
    if sc is None:
        return None
    try:
        if device_id:
            return sc.get_microphone(device_id, include_loopback=False)
    except Exception:
        pass
    try:
        for m in sc.all_microphones(include_loopback=False):
            return m
    except Exception:
        pass
    try:
        return sc.default_microphone()
    except Exception:
        return None


# ---------------------------------------------------------------- 采集线程
class _Capture(threading.Thread):
    def __init__(self, device, out_q: "queue.Queue", label: str, gain: float = 1.0):
        super().__init__(daemon=True, name=f"cap-{label}")
        self.device = device
        self.q = out_q
        self.label = label
        self.gain = gain
        self._stop = threading.Event()
        self.error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            with self.device.recorder(samplerate=SR, channels=1, blocksize=BLOCK) as rec:
                while not self._stop.is_set():
                    try:
                        data = rec.record(numframes=BLOCK)
                    except Exception as e:  # 设备被拔出 / 被独占
                        self.error = f"{self.label}: {e}"
                        data = np.zeros((BLOCK, 1), dtype=np.float32)
                        time.sleep(0.1)
                    x = np.asarray(data, dtype=np.float32).reshape(-1)
                    if x.size < BLOCK:
                        x = np.pad(x, (0, BLOCK - x.size))
                    elif x.size > BLOCK:
                        x = x[-BLOCK:]
                    if self.gain != 1.0:
                        x = x * self.gain
                    try:
                        self.q.put_nowait(x)
                    except queue.Full:
                        try:
                            self.q.get_nowait()
                            self.q.put_nowait(x)
                        except Exception:
                            pass
        except Exception as e:
            self.error = f"{self.label}: {e}"


# ---------------------------------------------------------------- VAD 切段
class VADSegmenter:
    """按 100ms block 做能量 VAD，输出完整语音片段。

    - 短于 min_len 的丢弃（咳嗽、键盘声）
    - 长于 max_len 强制切分，避免长句撑爆转写延迟
    - 保留 preroll，防止吞掉句首
    """

    def __init__(
        self,
        on_segment: Callable[[np.ndarray], None],
        threshold: float = 0.008,
        silence_sec: float = 0.7,
        min_len: float = 0.6,
        max_len: float = 20.0,
        preroll_sec: float = 0.4,
    ):
        self.on_segment = on_segment
        self.threshold = threshold
        self.silence_blocks = max(1, int(silence_sec * 1000 / BLOCK_MS))
        self.min_blocks = max(1, int(min_len * 1000 / BLOCK_MS))
        self.max_blocks = max(2, int(max_len * 1000 / BLOCK_MS))
        self.preroll_n = max(1, int(preroll_sec * 1000 / BLOCK_MS))

        self._pre: list[np.ndarray] = []
        self._buf: list[np.ndarray] = []
        self._silent_run = 0
        self._speech_run = 0
        self._active = False
        self.noise_floor = 0.002
        self._pending = 0

    def reset(self) -> None:
        self._pre.clear()
        self._buf.clear()
        self._silent_run = 0
        self._speech_run = 0
        self._active = False

    def _is_speech(self, rms: float) -> bool:
        return rms > max(self.threshold, self.noise_floor * 2.6)

    def feed(self, block: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(block)) + 1e-12))
        speech = self._is_speech(rms)

        # 自适应噪声底：只在被判定为静音时更新
        if not speech:
            self.noise_floor = 0.995 * self.noise_floor + 0.005 * min(rms, 0.05)
            self.noise_floor = max(0.0008, min(self.noise_floor, 0.05))

        if not self._active:
            self._pre.append(block)
            if len(self._pre) > self.preroll_n:
                self._pre.pop(0)
            self._speech_run = self._speech_run + 1 if speech else 0
            if self._speech_run >= 2:          # 连续 200ms 有声才敢认为是说话
                self._active = True
                self._buf = list(self._pre)
                self._pre.clear()
                self._silent_run = 0
            return

        self._buf.append(block)
        self._silent_run = 0 if speech else self._silent_run + 1

        if self._silent_run >= self.silence_blocks or len(self._buf) >= self.max_blocks:
            self._flush()

    def _flush(self) -> None:
        audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
        # 去掉尾部静音
        trim = max(0, len(audio) - self.silent_trim())
        if trim > 0 and self._silent_run > 0:
            audio = audio[:trim]
        self._buf = []
        self._active = False
        self._silent_run = 0
        self._speech_run = 0
        self._pre = []
        if audio.size >= int(BLOCK * self.min_blocks * 0.8):
            peak = float(np.max(np.abs(audio))) if audio.size else 0.0
            if peak > 1e-5:
                audio = np.clip(audio * (0.75 / max(peak, 0.75)), -1.0, 1.0)
                try:
                    self.on_segment(audio.astype(np.float32))
                except Exception:
                    pass

    def silent_trim(self) -> int:
        return max(0, (self._silent_run - 1)) * BLOCK

    def flush_pending(self) -> None:
        """外部强制结束（停止监听 / 会议结束）时把残留缓冲吐出来。"""
        if self._active and len(self._buf) >= self.min_blocks:
            self._silent_run = 1
            self._flush()


# ---------------------------------------------------------------- 总控
class AudioHub:
    def __init__(
        self,
        on_segment: Callable[[np.ndarray], None],
        on_level: Callable[[float, float], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.on_segment = on_segment
        self.on_level = on_level
        self.on_error = on_error
        self.segmenter = VADSegmenter(on_segment)
        self._threads: list[_Capture] = []
        self._mix_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.level_mic = 0.0
        self.level_sys = 0.0
        self.last_error: str | None = None

    def start(self, mic_enabled: bool, system_audio: bool, mic_id: str | None = None,
              mic_gain: float = 1.0, system_gain: float = 1.0) -> bool:
        if sc is None:
            self.last_error = "音频库 soundcard 未安装"
            if self.on_error:
                self.on_error(self.last_error)
            return False
        if self.running:
            return True

        self._stop.clear()
        self.segmenter.reset()
        self._threads = []
        q_mic: queue.Queue = queue.Queue(maxsize=8)
        q_sys: queue.Queue = queue.Queue(maxsize=8)

        mic_dev = _resolve_mic(mic_id) if mic_enabled else None
        sys_dev = _resolve_loopback() if system_audio else None

        if mic_enabled and mic_dev is None:
            self.last_error = "没有找到可用的麦克风"
        if system_audio and sys_dev is None:
            self.last_error = "没有找到系统声音回环设备"

        if mic_dev is not None:
            self._threads.append(_Capture(mic_dev, q_mic, "麦克风", mic_gain))
        if sys_dev is not None:
            self._threads.append(_Capture(sys_dev, q_sys, "系统声音", system_gain))

        if not self._threads:
            self.last_error = self.last_error or "未启用任何音源"
            if self.on_error:
                self.on_error(self.last_error)
            return False

        for t in self._threads:
            t.start()

        self.running = True
        self._mix_thread = threading.Thread(
            target=self._mix_loop, args=(q_mic, q_sys, mic_dev is not None, sys_dev is not None),
            daemon=True, name="mixer",
        )
        self._mix_thread.start()
        return True

    def _mix_loop(self, q_mic: queue.Queue, q_sys: queue.Queue, use_mic: bool, use_sys: bool) -> None:
        tick = BLOCK_MS / 1000.0
        next_t = time.monotonic()
        while not self._stop.is_set():
            next_t += tick
            a = self._latest(q_mic) if use_mic else None
            b = self._latest(q_sys) if use_sys else None

            if a is None and b is None:
                mixed = np.zeros(BLOCK, dtype=np.float32)
            elif a is None:
                mixed = b
            elif b is None:
                mixed = a
            else:
                n = min(a.size, b.size)
                mixed = np.clip(a[:n] + b[:n], -1.0, 1.0)
                if n < BLOCK:
                    mixed = np.pad(mixed, (0, BLOCK - n))

            self.level_mic = float(np.sqrt(np.mean(np.square(a)) + 1e-12)) if a is not None else 0.0
            self.level_sys = float(np.sqrt(np.mean(np.square(b)) + 1e-12)) if b is not None else 0.0
            if self.on_level:
                try:
                    self.on_level(self.level_mic, self.level_sys)
                except Exception:
                    pass

            self.segmenter.feed(mixed)

            sleep = next_t - time.monotonic()
            if sleep > 0:
                self._stop.wait(sleep)
            else:
                next_t = time.monotonic()

    @staticmethod
    def _latest(q: queue.Queue):
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.stop()
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads = []
        try:
            self.segmenter.flush_pending()
        except Exception:
            pass
        self.running = False
