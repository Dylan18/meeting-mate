"""本地语音识别引擎：sherpa-onnx（SenseVoice / Paraformer），CPU 实时转写。

选型说明：
- SenseVoice 支持中英日韩粤混说，自带标点与数字规范化（ITN），CPU 上非常快
- 比 Whisper small 快 5-10 倍，适合"全天监听"场景

两个容易踩的队列问题（已修）：
1. 退出时 `stop()` 只置 Event，worker 处理完手上一条就退出，队列里剩下的音频全丢。
   而界面上写的是"已记录的内容会自动保存"。现在改成投递哨兵 + join，队列按 FIFO
   自然排空，退出最多等 drain_timeout 秒。
2. 首次使用要下 1GB 模型，那几分钟里 `_rec is None`，每条音频都被直接 continue。
   现在先进有界 backlog（按秒数封顶），模型就绪后回灌，并在界面上说明。
"""
from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable

import numpy as np

from .config import config
from .log import get, log_exc

_HALLUCINATION_HINTS = (
    "请不吝点赞", "订阅", "打赏", "字幕由", "amara.org", "谢谢观看", "字幕组",
)
_SYMBOLS = set("♪♫，。、！？…,.!? 　~～-—：:；;“”\"'（）()【】[]\n\t")

# 模型加载期间最多替用户暂存多少秒音频；再多就真的只能丢了
_BACKLOG_SEC = 120.0
# 状态栏文案节流，避免每 100ms 刷一条事件
_STATUS_THROTTLE = 5.0


class Transcriber:
    def __init__(self, on_result: Callable[[dict], None],
                 on_status: Callable[[str, str], None] | None = None):
        self.on_result = on_result
        self.on_status = on_status
        self.status = "idle"          # idle | loading | ready | error
        self.status_text = "未加载"
        self.error: str | None = None
        self.latency_ms = 0.0
        self.dropped = 0              # 因未就绪/停止接收丢弃的片段数

        self._q: queue.Queue = queue.Queue()
        self._rec = None
        self._model_key: tuple | None = None
        self._lock = threading.Lock()
        self._accepting = True
        self._backlog: list[tuple[np.ndarray, dict]] = []
        self._backlog_sec = 0.0
        self._last_status_at = 0.0
        self._last_text = ""
        self._last_at = 0.0
        self._worker = threading.Thread(target=self._run, daemon=True, name="asr")
        self._worker.start()

    # ------------------------------------------------------------ 对外
    def submit(self, audio: np.ndarray, meta: dict) -> None:
        if audio is None or audio.size == 0:
            return
        if not self._accepting:
            self.dropped += 1
            return
        with self._lock:
            if self._rec is None:
                self._stash(audio, meta)
                return
        self._q.put((audio, meta, time.time()))

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._rec is not None

    def ensure_model(self, name: str) -> None:
        key = (name,)
        if self._model_key == key and self._rec is not None:
            return
        self._model_key = key
        self._q.put(("__load__", {"name": name}, time.time()))

    def stop(self, drain_timeout: float = 10.0) -> bool:
        """停止接收并排空队列。返回是否在超时内正常退出。

        哨兵法：队列是 FIFO，前面已经排队的片段会先被处理完，哨兵才轮到 —— 不需要
        自己数 qsize，也不会和正在进行的 put 打架。
        """
        self._accepting = False
        self._q.put(("__stop__", {}, time.time()))
        self._worker.join(timeout=drain_timeout)
        return not self._worker.is_alive()

    def pending(self) -> int:
        with self._lock:
            return self._q.qsize() + len(self._backlog)

    # ------------------------------------------------------------ 内部
    def _stash(self, audio: np.ndarray, meta: dict) -> None:
        """模型未就绪：按总秒数封顶暂存，超出丢最早的。"""
        dur = float(meta.get("dur") or audio.size / 16000.0)
        self._backlog.append((audio, meta))
        self._backlog_sec += dur
        while self._backlog and self._backlog_sec > _BACKLOG_SEC:
            _, old = self._backlog.pop(0)
            self._backlog_sec -= float(old.get("dur") or 0)
            self.dropped += 1
        self._notify_backlog()

    def _notify_backlog(self) -> None:
        now = time.time()
        if now - self._last_status_at < _STATUS_THROTTLE:
            return
        self._last_status_at = now
        if self.status == "loading":
            self._set_status(
                "loading",
                f"模型准备中，已暂存 {int(self._backlog_sec)} 秒音频，就绪后自动补转",
            )

    def _flush_backlog(self) -> None:
        with self._lock:
            items, self._backlog = self._backlog, []
            self._backlog_sec = 0.0
        for audio, meta in items:
            self._q.put((audio, meta, time.time()))
        if items:
            get("asr").info("模型就绪，补转暂存的 %d 段音频", len(items))

    def _set_status(self, status: str, text: str) -> None:
        self.status = status
        self.status_text = text
        if self.on_status:
            try:
                self.on_status(status, text)
            except Exception as e:
                log_exc("asr.on_status 回调异常", e)

    def _run(self) -> None:
        while True:
            try:
                audio, meta, enq = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            if isinstance(audio, str):
                if audio == "__stop__":
                    break
                if audio == "__load__":
                    self._load(meta["name"])
                continue

            with self._lock:
                rec = self._rec
            if rec is None:
                # 理论上到不了（submit 已经拦了），保险起见仍走暂存
                self._stash(audio, meta)
                continue

            try:
                t0 = time.time()
                text = self._decode(rec, audio)
                self.latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                self.error = str(e)
                self._set_status("error", f"识别失败：{e}")
                log_exc("asr.decode 失败", e)
                continue

            text = (text or "").strip()
            if not self._valid(text):
                continue

            self._last_text = text
            self._last_at = time.time()
            try:
                self.on_result({
                    "ts": meta.get("ts", enq),
                    "end": time.time(),
                    "text": text,
                    "scope": meta.get("scope", "daily"),
                    "meeting_id": meta.get("meeting_id"),
                    "dur": round(float(meta.get("dur", 0)), 2),
                })
            except Exception as e:
                log_exc("asr.on_result 回调异常（转写结果可能丢失）", e)

    def _load(self, name: str) -> None:
        try:
            import sherpa_onnx
        except Exception as e:
            self.error = str(e)
            self._set_status("error", f"sherpa-onnx 未安装：{e}")
            log_exc("asr 导入 sherpa_onnx 失败", e)
            return
        try:
            from .model_fetch import ensure_model, entry
            t0 = time.time()
            self._set_status("loading", "检查模型…")
            d = ensure_model(name, progress=lambda s: self._set_status("loading", s))
            threads = max(2, min(6, (os.cpu_count() or 4) // 2))
            self._set_status("loading", "加载识别模型…")

            if "sensevoice" in name:
                lang = str(config.get("asr_language", "auto") or "auto")
                rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(d / "model.int8.onnx"),
                    tokens=str(d / "tokens.txt"),
                    num_threads=threads,
                    use_itn=True,
                    language=lang,
                    debug=False,
                )
            else:
                rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    paraformer=str(d / "model.int8.onnx"),
                    tokens=str(d / "tokens.txt"),
                    num_threads=threads,
                    debug=False,
                )
            with self._lock:
                self._rec = rec
            self.error = None
            self._set_status("ready", f"{entry(name)['title'].split('（')[0]} 就绪（{time.time() - t0:.1f}s）")
            self._flush_backlog()
        except Exception as e:
            self.error = str(e)
            self._set_status("error", f"模型加载失败：{e}")
            log_exc("asr 模型加载失败", e)

    @staticmethod
    def _decode(rec, audio: np.ndarray) -> str:
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        stream = rec.create_stream()
        stream.accept_waveform(16000, x)
        rec.decode_stream(stream)
        return getattr(stream.result, "text", "") or ""

    def _valid(self, text: str) -> bool:
        if not text:
            return False
        core = "".join(ch for ch in text if ch not in _SYMBOLS)
        if len(core) <= 1:
            return False
        if len(core) <= 2 and core in ("嗯", "啊", "哦", "呃", "好", "对", "是的", "OK"):
            return False
        for hint in _HALLUCINATION_HINTS:
            if hint in text:
                return False
        return not (text == self._last_text and (time.time() - self._last_at) < 3.0)
