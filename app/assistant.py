"""业务编排：把采集、转写、存储、总结串起来。

并发模型（之前这里是最大的坑）：
- 所有"生成类"任务（日报 / 会议纪要）共用一把 `_job_lock`
- 入口用 `acquire(blocking=False)` 在同一把锁内完成"检查 + 占位"，杜绝
  check-then-act 竞态；以前 self.busy 在调用线程检查、worker 线程才置位，
  双击按钮能同时起两个 LLM 任务，同一天的日报被后完成的覆盖
- 释放统一放在 worker 的 finally，异常路径也不会把锁吞掉
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from datetime import date, datetime

from . import llm, store
from .asr import Transcriber
from .audio import AudioHub
from .config import config
from .log import get, log_exc

Event = Callable[[str, object], None]

_log = get("assistant")


def _emit(fn: Event | None, kind: str, payload=None) -> None:
    if fn:
        try:
            fn(kind, payload)
        except Exception as e:
            log_exc(f"assistant 事件回调异常({kind})", e)


class Assistant:
    def __init__(self, on_event: Event):
        self.on_event = on_event
        self.transcriber = Transcriber(self._on_text, self._on_asr_status)
        self.hub = AudioHub(self._on_audio, self._on_level, self._on_audio_error,
                            self._on_audio_recovered)
        self.listening = False
        self.meeting: dict | None = None
        # 按天存，避免进程连跑两天时第二天的日报串进前一天的会议标题
        self.meeting_titles: dict[str, list[str]] = {}
        self.busy = False
        self.audio_error: str | None = None
        self._job_lock = threading.Lock()
        self._store_error_reported = False

    # ------------------------------------------------------------ 转写回调
    def _on_asr_status(self, status: str, text: str) -> None:
        _emit(self.on_event, "asr_status", {"status": status, "text": text})

    def _on_text(self, rec: dict) -> None:
        try:
            store.append_segment(rec)
            self._store_error_reported = False
        except Exception as e:
            # 落盘失败是用户真正丢数据的地方，必须留痕并让界面看见
            log_exc("store.append_segment 失败（该段转写未保存）", e)
            if not self._store_error_reported:
                self._store_error_reported = True
                _emit(self.on_event, "error", f"记录保存失败，请检查磁盘：{e}")
        _emit(self.on_event, "text", rec)

    def _on_level(self, mic: float, sys_: float) -> None:
        _emit(self.on_event, "level", (mic, sys_))

    def _on_audio_error(self, msg: str) -> None:
        self.audio_error = msg
        _emit(self.on_event, "audio_state", {"ok": False, "msg": msg})
        _emit(self.on_event, "error", msg)

    def _on_audio_recovered(self) -> None:
        self.audio_error = None
        _emit(self.on_event, "audio_state", {"ok": True, "msg": ""})
        _emit(self.on_event, "notice", "音频设备已恢复")

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

    # ------------------------------------------------------------ 任务锁
    def _try_begin(self, label: str) -> bool:
        if not self._job_lock.acquire(blocking=False):
            return False
        self.busy = True
        _emit(self.on_event, "busy", label)
        return True

    def _begin_waiting(self, label: str, timeout: float = 240.0) -> bool:
        if not self._job_lock.acquire(timeout=timeout):
            return False
        self.busy = True
        _emit(self.on_event, "busy", label)
        return True

    def _end_job(self) -> None:
        self.busy = False
        _emit(self.on_event, "busy", "")
        try:
            self._job_lock.release()
        except RuntimeError:
            log_exc("assistant._end_job 重复释放", "job lock 不在锁定状态")

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
        self.audio_error = self.hub.audio_error
        model = config.get("asr_model", "sensevoice")
        self.transcriber.ensure_model(model)
        if not self.transcriber.ready:
            _emit(self.on_event, "notice", self._model_pending_text(model))
        _emit(self.on_event, "listening", True)
        return True

    @staticmethod
    def _model_pending_text(model: str) -> str:
        """本地已有模型只是加载几秒，和首次要下 1GB，提示必须分开说。"""
        from .model_fetch import is_ready
        if is_ready(model):
            return "正在加载识别模型，几秒后开始转写（这期间的语音会先暂存）"
        return "首次使用需下载识别模型（约 1GB），这几分钟的语音会先暂存、就绪后自动补转"

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

    def restart_listening(self) -> None:
        """设置里改了音源 / 设备 / 语言后，只有真正重启采集才生效。"""
        if not self.listening:
            return
        self.stop_listening()
        self.start_listening()

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
        # 排队而非抢占：日报在跑时等它结束，而不是把 busy 覆盖掉
        if not self._begin_waiting("正在生成会议纪要…"):
            _emit(self.on_event, "error", "会议纪要生成失败：等待其他任务超时")
            return
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
            path = store.write_meeting_report(m["start"], title or "", body, {
                "end": m["end"],
                "duration": dur_txt,
                "segments": len(recs),
            })
            day = m["start"].strftime("%Y-%m-%d")
            if title:
                self.meeting_titles.setdefault(day, []).append(title)
            _emit(self.on_event, "report_ready", {"path": str(path), "kind": "meeting", "title": title})
        except Exception as e:
            log_exc("assistant 生成会议纪要失败", e)
            _emit(self.on_event, "error", f"会议纪要生成失败：{e}")
        finally:
            self._end_job()

    # ------------------------------------------------------------ 日报
    def generate_daily(self, day: str | None = None, silent_if_empty: bool = True) -> bool:
        """返回是否真的启动了任务（调用方可据此提示"已有任务在跑"）。"""
        if not self._try_begin("正在整理今天的记录…"):
            _emit(self.on_event, "notice", "还有任务在进行，请稍候再试。")
            return False

        def work() -> None:
            day_label = day or date.today().strftime("%Y-%m-%d")
            try:
                self._wait_queue(timeout=60)
                recs = [r for r in store.load_segments(day) if r.get("scope") != "meeting"]
                text = store.render(recs)
                # 会议内容以前完全不进日报：开一天会的人日报正文是"没有记录到内容"
                digests = store.meeting_digests_for(day_label)
                if not text.strip() and not digests:
                    if not silent_if_empty:
                        _emit(self.on_event, "notice", f"{day_label} 没有记录到内容。")
                    return
                if llm.llm_ready():
                    titles = self.meeting_titles.get(day_label) or store.meeting_titles_for(day_label)
                    body = llm.summarize_day(
                        text, day_label, meeting_titles=titles or None,
                        meeting_digests=digests,
                        progress=lambda s: _emit(self.on_event, "busy", s),
                    )
                else:
                    body = llm.fallback_daily(recs, day_label, digests=digests)
                path = store.write_daily_report(day_label, body, {"segments": len(recs)})
                _emit(self.on_event, "report_ready",
                      {"path": str(path), "kind": "daily", "title": f"{day_label} 日报"})
            except Exception as e:
                log_exc("assistant 生成日报失败", e)
                _emit(self.on_event, "error", f"日报生成失败：{e}")
                _emit(self.on_event, "daily_failed", {"day": day_label, "msg": str(e)[:160]})
            finally:
                self._end_job()

        threading.Thread(target=work, daemon=True, name="daily-report").start()
        return True

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
        _emit(self.on_event, "busy", "正在保存最后几条记录…")
        try:
            self.stop_listening()
        except Exception as e:
            log_exc("assistant.shutdown stop_listening 失败", e)
        ok = True
        try:
            ok = self.transcriber.stop(drain_timeout=10.0)
        except Exception as e:
            log_exc("assistant.shutdown transcriber.stop 失败", e)
        if not ok:
            _log.warning("退出时转写队列未在 10s 内排空，仍有 %d 段未处理",
                         self.transcriber.pending())


def _extract_title(body: str) -> str | None:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            t = line[2:].strip().strip("#").strip()
            if t:
                return t[:40]
    return None
