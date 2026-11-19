"""本地语音识别引擎：sherpa-onnx（SenseVoice / Paraformer），CPU 实时转写。

选型说明：
- SenseVoice 支持中英日韩粤混说，自带标点与数字规范化（ITN），CPU 上非常快
- 比 Whisper small 快 5-10 倍，适合"全天监听"场景
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Callable

import numpy as np

from .config import config

_HALLUCINATION_HINTS = (
    "请不吝点赞", "订阅", "打赏", "字幕由", "amara.org", "谢谢观看", "字幕组",
)
_SYMBOLS = set("♪♫，。、！？…,.!? 　~～-—：:；;“”\"'（）()【】[]\n\t")


class Transcriber:
    def __init__(self, on_result: Callable[[dict], None],
                 on_status: Callable[[str, str], None] | None = None):
        self.on_result = on_result
        self.on_status = on_status
        self.status = "idle"          # idle | loading | ready | error
        self.status_text = "未加载"
        self.error: str | None = None
        self.latency_ms = 0.0

        self._q: "queue.Queue" = queue.Queue()
        self._rec = None
        self._model_key: tuple | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_text = ""
        self._last_at = 0.0
        self._worker = threading.Thread(target=self._run, daemon=True, name="asr")
        self._worker.start()

    # ------------------------------------------------------------ 对外
    def submit(self, audio: np.ndarray, meta: dict) -> None:
        if audio is None or audio.size == 0:
            return
        self._q.put((audio, meta, time.time()))

    def ensure_model(self, name: str) -> None:
        key = (name,)
        if self._model_key == key and self._rec is not None:
            return
        self._model_key = key
        self._q.put(("__load__", {"name": name}, time.time()))

    def stop(self) -> None:
        self._stop.set()

    def pending(self) -> int:
        return self._q.qsize()

    # ------------------------------------------------------------ 内部
    def _set_status(self, status: str, text: str) -> None:
        self.status = status
        self.status_text = text
        if self.on_status:
            try:
                self.on_status(status, text)
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                audio, meta, enq = self._q.get(timeout=0.5)
            except queue.Empty:
                continue

            if isinstance(audio, str) and audio == "__load__":
                self._load(meta["name"])
                continue

            with self._lock:
                rec = self._rec
            if rec is None:
                continue

            try:
                t0 = time.time()
                text = self._decode(rec, audio)
                self.latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                self.error = str(e)
                self._set_status("error", f"识别失败：{e}")
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
            except Exception:
                pass

    def _load(self, name: str) -> None:
        try:
            import sherpa_onnx
        except Exception as e:
            self._set_status("error", f"sherpa-onnx 未安装：{e}")
            self.error = str(e)
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
        except Exception as e:
            self.error = str(e)
            self._set_status("error", f"模型加载失败：{e}")

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
        if text == self._last_text and (time.time() - self._last_at) < 3.0:
            return False
        return True
