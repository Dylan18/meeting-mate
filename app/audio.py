"""音频采集：麦克风 + 系统回环双路并行采集、混音、VAD 自动切段。

设计要点：
- 每路一个采集线程，各自独立阻塞读取，避免互相拖累
- 混音线程按 100ms 固定节拍取最新帧，队列满则丢旧帧（保证实时性优先）
- 能量 VAD + 启动校准 + 稳态噪声兜底，自动切成一句一句的语音片段送给转写引擎

关于 VAD 的两个坑（都有实测复现）：
1. 噪声底只在"判定为静音"时更新，而"静音"又依赖噪声底 —— 自锁。办公室空调让底噪
   到了 0.01，阈值 0.008，于是永远判为"有人说话"，噪声底再也降不下来。修法：启动
   先校准 2s 直接定调，之后"明显低于噪声底"的帧快速跟随，"可能是语音"的帧几乎不动。
2. 环境里有稳态噪音（风扇、机械键盘）时，光靠能量分不开。加一道统计兜底：进入语音
   态超过 6s 且能量变异系数极低，就认定是稳态噪声，抬升噪声底并整段丢弃。

采集线程的错误以前只写进 `_Capture.error` 没人读，麦克风被拔掉后界面还是绿点。
现在 mixer 每 100ms 巡检一次，连续失败/线程已死都会通过 on_error 上报给 UI。
"""
from __future__ import annotations

import queue
import statistics
import threading
import time
from collections.abc import Callable

import numpy as np

from .log import log_exc

try:
    import soundcard as sc
except Exception:  # pragma: no cover
    sc = None

SR = 16000          # 统一采样率
BLOCK_MS = 100
BLOCK = SR * BLOCK_MS // 1000   # 1600 frames

# 采集线程连续失败多少块后上报（0.1s 一块 → 2s）
_CAPTURE_FAIL_REPORT = 20


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
    except Exception as e:
        log_exc("audio.list_devices 枚举失败", e)

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
    except Exception as e:
        log_exc("audio._resolve_loopback 枚举失败", e)
    try:
        spk = sc.default_speaker()
        if spk:
            return sc.get_microphone(spk.id, include_loopback=True)
    except Exception as e:
        log_exc("audio._resolve_loopback 取默认扬声器失败", e)
    return None


def _resolve_mic(device_id: str | None):
    if sc is None:
        return None
    try:
        if device_id:
            return sc.get_microphone(device_id, include_loopback=False)
    except Exception as e:
        log_exc(f"audio._resolve_mic 指定设备不可用({device_id})", e)
    try:
        for m in sc.all_microphones(include_loopback=False):
            return m
    except Exception as e:
        log_exc("audio._resolve_mic 枚举失败", e)
    try:
        return sc.default_microphone()
    except Exception as e:
        log_exc("audio._resolve_mic 取默认麦克风失败", e)
        return None


# ---------------------------------------------------------------- 采集线程
class _Capture(threading.Thread):
    def __init__(self, device, out_q: queue.Queue, label: str, gain: float = 1.0):
        super().__init__(daemon=True, name=f"cap-{label}")
        self.device = device
        self.q = out_q
        self.label = label
        self.gain = gain
        self._stop = threading.Event()
        self.error: str | None = None
        self.fail_streak = 0      # 连续读失败次数，成功一次清零
        self.reported = False     # 同一轮故障只上报一次，避免刷屏

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            with self.device.recorder(samplerate=SR, channels=1, blocksize=BLOCK) as rec:
                while not self._stop.is_set():
                    try:
                        data = rec.record(numframes=BLOCK)
                        if self.fail_streak:
                            self.fail_streak = 0
                            self.error = None
                            self.reported = False
                    except Exception as e:  # 设备被拔出 / 被独占
                        self.fail_streak += 1
                        self.error = f"{self.label}：{e}"
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
                        except Exception as e:
                            log_exc(f"audio._Capture({self.label}) 队列替换失败", e)
        except Exception as e:
            self.error = f"{self.label}：{e}"
            self.fail_streak = max(self.fail_streak, _CAPTURE_FAIL_REPORT)
            log_exc(f"audio._Capture({self.label}) 线程退出", e)


# ---------------------------------------------------------------- VAD 切段
def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _calibrate(samples: list[float]) -> float:
    """校准噪声底：取低分位数而不是中位数。

    用中位数的问题是：用户点"开始监听"时如果正在说话，中位数就是语音能量，阈值会被
    顶到天上，整场会议都识别不到。低分位数取的是"最安静的那几帧"，天然更接近真实底噪；
    再压一个 0.02 的上限，保证极端环境下也不至于把正常说话挡在门外。
    """
    floor = _percentile(samples, 0.15)
    return max(0.0008, min(floor, 0.02))


class VADSegmenter:
    """按 100ms block 做能量 VAD，输出完整语音片段。

    - 启动先校准 calib_sec 秒，用中位数定噪声底（校准期不出片段）
    - 短于 min_len 的丢弃（咳嗽、键盘声）
    - 长于 max_len 强制切分，避免长句撑爆转写延迟
    - 保留 preroll，防止吞掉句首
    - 稳态噪声兜底：能量变异系数过低说明不是人在说话
    """

    def __init__(
        self,
        on_segment: Callable[[np.ndarray], None],
        threshold: float = 0.008,
        silence_sec: float = 0.7,
        min_len: float = 0.6,
        max_len: float = 20.0,
        preroll_sec: float = 0.4,
        calib_sec: float = 2.0,
    ):
        self.on_segment = on_segment
        self.threshold = threshold
        self.silence_blocks = max(1, int(silence_sec * 1000 / BLOCK_MS))
        self.min_blocks = max(1, int(min_len * 1000 / BLOCK_MS))
        self.max_blocks = max(2, int(max_len * 1000 / BLOCK_MS))
        self.preroll_n = max(1, int(preroll_sec * 1000 / BLOCK_MS))
        self.calib_blocks = max(0, int(calib_sec * 1000 / BLOCK_MS))

        self._pre: list[np.ndarray] = []
        self._buf: list[np.ndarray] = []
        self._silent_run = 0
        self._speech_run = 0
        self._active = False
        self.noise_floor = 0.002
        self._calib: list[float] = []
        self._rms_hist: list[float] = []
        self._since_check = 0
        self._lock = threading.RLock()
        self.dropped_noise = 0     # 因判定为稳态噪声被丢弃的片段数（测试与诊断用）

    def reset(self) -> None:
        with self._lock:
            self._pre.clear()
            self._buf.clear()
            self._silent_run = 0
            self._speech_run = 0
            self._active = False
            self.noise_floor = 0.002
            self._calib.clear()
            self._rms_hist.clear()
            self._since_check = 0

    # -------------------------------------------------------- 判定
    def _is_speech(self, rms: float) -> bool:
        return rms > max(self.threshold, self.noise_floor * 2.6)

    def _track_noise(self, rms: float) -> None:
        """噪声底跟踪：明显是背景的帧快速跟随，可能是语音的帧几乎冻结。

        以前只在"判定为静音"时更新，导致"噪声太大 → 永远不是静音 → 噪声底永不更新"
        的自锁。改成不依赖判定结果即可打破循环。
        """
        if rms <= self.noise_floor * 1.6:
            self.noise_floor = 0.9 * self.noise_floor + 0.1 * rms
        else:
            # 0.0005 的上升速率约等于 200 秒才翻倍，说话不会把底噪顶上去
            self.noise_floor = 0.9995 * self.noise_floor + 0.0005 * min(rms, 0.05)
        self.noise_floor = max(0.0008, min(self.noise_floor, 0.05))

    def _is_stationary_noise(self) -> bool:
        """能量长期平稳 → 风扇/空调/机械键盘，不是人声。

        人说话的能量起伏很大（音节、停顿、重音），30 个 100ms 帧的变异系数通常在
        0.5 以上；稳态噪声一般在 0.15 以内。这个判据只用能量，不需要频谱，够用且便宜。
        """
        if len(self._buf) < 60 or len(self._rms_hist) < 30:
            return False
        mean = statistics.fmean(self._rms_hist)
        if mean <= 0:
            return True
        cv = statistics.pstdev(self._rms_hist) / mean
        return cv < 0.15 and mean < 0.06

    # -------------------------------------------------------- 主入口
    def feed(self, block: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(block)) + 1e-12))
        with self._lock:
            # ---- 启动校准 ----
            if len(self._calib) < self.calib_blocks:
                self._calib.append(rms)
                if len(self._calib) == self.calib_blocks and self._calib:
                    self.noise_floor = _calibrate(self._calib)
                return

            speech = self._is_speech(rms)
            self._track_noise(rms)

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
                    self._rms_hist.clear()
                return

            self._buf.append(block)
            self._rms_hist.append(rms)
            if len(self._rms_hist) > 40:
                self._rms_hist.pop(0)
            self._silent_run = 0 if speech else self._silent_run + 1

            self._since_check += 1
            if self._since_check >= 10:
                self._since_check = 0
                if self._is_stationary_noise():
                    # 稳态噪声：抬升噪声底到观测水平，整段丢弃
                    self.noise_floor = max(self.noise_floor, min(rms, 0.05))
                    self.dropped_noise += 1
                    self._discard()
                    return

            if self._silent_run >= self.silence_blocks or len(self._buf) >= self.max_blocks:
                self._flush()

    def _discard(self) -> None:
        self._buf = []
        self._active = False
        self._silent_run = 0
        self._speech_run = 0
        self._pre = []
        self._rms_hist = []

    def _flush(self) -> None:
        if not self._buf:
            self._discard()
            return
        audio = np.concatenate(self._buf)
        # 去掉尾部静音
        if self._silent_run > 0:
            trim = (self._silent_run - 1) * BLOCK
            if trim > 0 and audio.size > trim:
                audio = audio[:-trim]
        self._buf = []
        self._active = False
        self._silent_run = 0
        self._speech_run = 0
        self._pre = []
        self._rms_hist = []

        if audio.size < int(BLOCK * self.min_blocks * 0.8):
            return
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak <= 1e-5:
            return
        audio = np.clip(audio * (0.75 / max(peak, 0.75)), -1.0, 1.0)
        try:
            self.on_segment(audio.astype(np.float32))
        except Exception as e:
            log_exc("audio.on_segment 回调异常", e)

    def flush_pending(self) -> None:
        """外部强制结束（停止监听 / 会议结束）时把残留缓冲吐出来。"""
        with self._lock:
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
        on_recovered: Callable[[], None] | None = None,
    ):
        self.on_segment = on_segment
        self.on_level = on_level
        self.on_error = on_error
        self.on_recovered = on_recovered
        self.segmenter = VADSegmenter(on_segment)
        self._threads: list[_Capture] = []
        self._mix_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.level_mic = 0.0
        self.level_sys = 0.0
        self.last_error: str | None = None
        self.audio_error: str | None = None   # 运行期降级原因，供 UI 展示

    # -------------------------------------------------------- 生命周期
    def start(self, mic_enabled: bool, system_audio: bool, mic_id: str | None = None,
              mic_gain: float = 1.0, system_gain: float = 1.0) -> bool:
        if sc is None:
            return self._fail("音频库 soundcard 未安装")
        if self.running:
            return True

        self._stop.clear()
        self.segmenter.reset()
        self._threads = []
        self.audio_error = None
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
            return self._fail(self.last_error or "未启用任何音源")

        for t in self._threads:
            t.start()

        self.running = True
        self._mix_thread = threading.Thread(
            target=self._mix_loop, args=(q_mic, q_sys, mic_dev is not None, sys_dev is not None),
            daemon=True, name="mixer",
        )
        self._mix_thread.start()
        return True

    def _fail(self, msg: str) -> bool:
        self.last_error = msg
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception as e:
                log_exc("audio.on_error 回调异常", e)
        return False

    def stop(self) -> None:
        self._stop.set()
        # 先把 mixer 停下来，再 flush —— 否则 flush_pending 会和 mixer 里的 feed 抢
        # 同一份 _buf，同一段音频可能被吐两次、或刚追加的块直接丢。
        mix = self._mix_thread
        if mix is not None:
            mix.join(timeout=1.5)
            self._mix_thread = None
        for t in self._threads:
            t.stop()
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads = []
        try:
            self.segmenter.flush_pending()
        except Exception as e:
            log_exc("audio.stop flush_pending 异常", e)
        self.running = False

    # -------------------------------------------------------- 混音
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
                except Exception as e:
                    log_exc("audio.on_level 回调异常", e)

            try:
                self.segmenter.feed(mixed)
            except Exception as e:
                log_exc("audio.segmenter.feed 异常", e)

            self._check_captures()

            sleep = next_t - time.monotonic()
            if sleep > 0:
                self._stop.wait(sleep)
            else:
                next_t = time.monotonic()

    def _check_captures(self) -> None:
        """采集线程死了 / 连续读失败，必须让用户知道，否则就是"绿点但没录到"。"""
        if self._stop.is_set():
            return
        healthy = True
        for t in self._threads:
            dead = not t.is_alive()
            failing = t.fail_streak >= _CAPTURE_FAIL_REPORT
            if not (dead or failing):
                continue
            healthy = False
            if t.reported and not dead:
                continue
            t.reported = True
            if dead:
                msg = f"{t.label}采集已中断，请检查设备后重新开始监听"
            else:
                msg = f"{t.label}读取失败（设备被拔出或被其他程序独占），请检查设备"
            self.audio_error = msg
            if self.on_error:
                try:
                    self.on_error(msg)
                except Exception as e:
                    log_exc("audio.on_error 回调异常", e)
            return
        # 故障恢复：设备插回来了，状态点和提示要跟着回绿
        if healthy and self.audio_error:
            self.audio_error = None
            if self.on_recovered:
                try:
                    self.on_recovered()
                except Exception as e:
                    log_exc("audio.on_recovered 回调异常", e)

    @staticmethod
    def _latest(q: queue.Queue):
        item = None
        try:
            while True:
                item = q.get_nowait()
        except queue.Empty:
            pass
        return item
