"""业务编排：把采集、转写、存储、总结串起来。"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import date, datetime
from typing import Callable

from . import llm, store
from .asr import Transcriber
from .audio import AudioHub
from .config import config

Event = Callable[[str, object], None]


def _emit(fn: Event | None, kind: str, payload=None) -> None:
    if fn:
        try:
            fn(kind, payload)
        except Exception:
            pass


class Assistant:
    def __init__(self, on_event: Event):
        self.on_event = on_event
        self.transcriber = Transcriber(self._on_text, self._on_asr_status)
        self.hub = AudioHub(self._on_audio, self._on_level, self._on_audio_error)
        self.listening = False
        self.meeting: dict | None = None
        self.meeting_titles: list[str] = []
        self.busy = False
        self._last_err = ""

    # ------------------------------------------------------------ 转写回调
    def _on_asr_status(self, status: str, text: str) -> None:
        _emit(self.on_event, "asr_status", {"status": status, "text": text})

    def _on_text(self, rec: dict) -> None:
        try:
            store.append_segment(rec)
        except Exception:
            pass
        _emit(self.on_event, "text", rec)

    def _on_level(self, mic: float, sys_: float) -> None:
        _emit(self.on_event, "level", (mic, sys_))

    def _on_audio_error(self, msg: str) -> None:
        _emit(self.on_event, "error", msg)

    def _on_audio(self, audio) -> None:
        dur = len(audio) / 16000.0
        scope = "meeting" if self.meeting else "daily"
        self.transcriber.submit(
            audio,
            {
                "ts": time.time() - dur,
                "dur": dur,
                "scope": scope,
                "meeting_id": self.meeting["id"] if self.meeting else None,
            },
        )
        _emit(self.on_event, "heard", dur)

    # ------------------------------------------------------------ 监听
    def start_listening(self) -> bool:
        if self.listening:
            return True
        ok = self.hub.start(
            mic_enabled=config.get("mic_enabled", True),
            system_audio=config.get("system_audio", True),
            mic_id=config.get("mic_device_id"),
            mic_gain=float(config.get("mic_gain", 1.0)),
            system_gain=float(config.get("system_gain", 1.0)),
        )
        if not ok:
            _emit(self.on_event, "error", self.hub.last_error or "启动录音失败")
            return False
        self.listening = True
        self.transcriber.ensure_model(config.get("asr_model", "sensevoice"))
        _emit(self.on_event, "listening", True)
        return True

    def stop_listening(self) -> None:
        if not self.listening:
            return
        self.hub.stop()
        self.listening = False
        _emit(self.on_event, "listening", False)

    def toggle_listening(self) -> bool:
        if self.listening:
            self.stop_listening()
            return False
        return self.start_listening()

    # ------------------------------------------------------------ 会议
    def start_meeting(self, topic: str = "") -> dict:
        if self.meeting:
            return self.meeting
        if not self.listening:
            self.start_listening()
        self.meeting = {
            "id": uuid.uuid4().hex[:8],
            "start": datetime.now(),
            "topic": topic.strip(),
        }
        _emit(self.on_event, "meeting_started", self.meeting)
        return self.meeting

    def end_meeting(self, auto_summary: bool = True) -> None:
        if not self.meeting:
            return
        m = self.meeting
        m["end"] = datetime.now()
        self.meeting = None
        _emit(self.on_event, "meeting_ended", m)

        def work() -> None:
            self._wait_queue(timeout=90)
            if not auto_summary:
                return
            self._summarize_meeting(m)

        threading.Thread(target=work, daemon=True, name="end-meeting").start()

    def _summarize_meeting(self, m: dict) -> None:
        self.busy = True
        _emit(self.on_event, "busy", "正在生成会议纪要…")
        try:
            recs = store.segments_between(m["start"], m["end"])
            text = store.render(recs)
            if not text.strip():
                _emit(self.on_event, "notice", "这场会议没有识别到有效语音内容。")
                return
            title = None
            if llm.llm_ready():
                body = llm.summarize_meeting(
                    text, m["start"], m["end"], m.get("topic") or None,
                    progress=lambda s: _emit(self.on_event, "busy", s),
                )
                title = _extract_title(body)
            else:
                body = (
                    "## 会议摘要\n未配置大模型，以下为原始转写内容。\n\n"
                    f"## 转写原文\n{text}\n"
                )
            secs = int((m["end"] - m["start"]).total_seconds())
            dur_txt = f"{secs // 60} 分钟" if secs >= 60 else f"{max(1, secs)} 秒"
            path = store.meeting_report_path(m["start"], title or "")
            header = (
                f"# {title or '会议纪要'}\n\n"
                f"> {m['start'].strftime('%Y-%m-%d %H:%M')} — {m['end'].strftime('%H:%M')}"
                f" ｜ 时长 {dur_txt}"
                f" ｜ 转写 {len(recs)} 段\n\n"
            )
            clean = body
            if clean.lstrip().startswith("# "):
                clean = "\n".join(clean.splitlines()[1:]).strip()
            path.write_text(header + clean + "\n", encoding="utf-8")
            if title:
                self.meeting_titles.append(title)
            _emit(self.on_event, "report_ready", {"path": str(path), "kind": "meeting", "title": title})
        except Exception as e:
            _emit(self.on_event, "error", f"会议纪要生成失败：{e}")
        finally:
            self.busy = False
            _emit(self.on_event, "busy", "")

    # ------------------------------------------------------------ 日报
    def generate_daily(self, day: str | None = None, silent_if_empty: bool = True) -> None:
        if self.busy:
            _emit(self.on_event, "notice", "还有任务在进行，请稍候再试。")
            return

        def work() -> None:
            self.busy = True
            day_label = day or date.today().strftime("%Y-%m-%d")
            try:
                _emit(self.on_event, "busy", "正在整理今天的记录…")
                self._wait_queue(timeout=60)
                recs = [r for r in store.load_segments(day) if r.get("scope") != "meeting"]
                text = store.render(recs)
                if not text.strip():
                    if not silent_if_empty:
                        _emit(self.on_event, "notice", f"{day_label} 没有记录到内容。")
                    return
                if llm.llm_ready():
                    titles = self.meeting_titles or store.meeting_titles_for(day_label)
                    body = llm.summarize_day(
                        text, day_label, meeting_titles=titles or None,
                        progress=lambda s: _emit(self.on_event, "busy", s),
                    )
                else:
                    body = llm.fallback_daily(recs, day_label)
                path = store.report_path_for(day_label, "日报")
                header = (
                    f"# {day_label} 工作日报\n\n"
                    f"> 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                    f" ｜ 语音片段 {len(recs)} 条\n\n"
                )
                path.write_text(header + body.strip() + "\n", encoding="utf-8")
                _emit(self.on_event, "report_ready", {"path": str(path), "kind": "daily", "title": f"{day_label} 日报"})
            except Exception as e:
                _emit(self.on_event, "error", f"日报生成失败：{e}")
            finally:
                self.busy = False
                _emit(self.on_event, "busy", "")

        threading.Thread(target=work, daemon=True, name="daily-report").start()

    # ------------------------------------------------------------ 工具
    def _wait_queue(self, timeout: float = 60) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.transcriber.pending() == 0:
                time.sleep(0.5)
                if self.transcriber.pending() == 0:
                    return
            time.sleep(0.3)

    def shutdown(self) -> None:
        try:
            self.stop_listening()
        except Exception:
            pass
        try:
            self.transcriber.stop()
        except Exception:
            pass


def _extract_title(body: str) -> str | None:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            t = line[2:].strip().strip("#").strip()
            if t:
                return t[:40]
    return None
