"""前后端桥接：把 Assistant 的事件流暴露给 WebView 前端。"""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .. import llm, store
from ..assistant import Assistant
from ..audio import list_devices
from ..config import DATA_DIR, LOG_DIR, REPORT_DIR, config, parse_hhmm
from ..log import log_exc

# 改了这些字段必须重启采集/重载模型才生效，否则界面显示"已保存"但行为没变
_RESTART_KEYS = {"asr_model", "asr_language", "mic_device_id", "mic_enabled", "system_audio"}
# 日报生成失败后的自动重试间隔（秒），避免每 60s 白烧一次 API
_DAILY_RETRY_BACKOFF = 30 * 60


def _jsonable(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (tuple, list)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    return obj


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


class Bridge:
    def __init__(self, start_scheduler: bool = True):
        self.events: list[dict] = []
        self._seq = 0
        self._dropped_below = 0      # 已被裁掉的最大 seq，用于判断前端是否漏了事件
        self._lock = threading.RLock()
        self._window = None
        self._asst = Assistant(self._on_event)
        self.count_today = len(store.load_segments())
        self._closing = False
        self._daily_fail_at: dict[str, float] = {}
        # 测试里传 False：否则调度线程会在后台触发真实的定时日报（会调 LLM、写报告）
        if start_scheduler:
            threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()

    def attach_window(self, window) -> None:
        """显式挂载窗口。

        注意：不能叫 self.window —— pywebview 会反射 js_api 对象的公开属性，
        撞上 Window/Form 这类 .NET 对象会递归爆栈。
        """
        self._window = window

    # ------------------------------------------------------------ 事件
    def _on_event(self, kind: str, payload) -> None:
        if kind == "daily_failed":
            day = (payload or {}).get("day") or date.today().strftime("%Y-%m-%d")
            self._daily_fail_at[day] = time.time()
        self._push(kind, payload)

    def _push(self, kind: str, payload=None) -> None:
        with self._lock:
            self._seq += 1
            # 电平事件频率高，只保留最新一条，避免事件流膨胀
            if (kind == "level" and self.events and self.events[-1]["kind"] == "level"):
                self.events[-1] = {"seq": self._seq, "kind": kind, "data": _jsonable(payload)}
                return
            if kind == "text":
                self.count_today += 1
            self.events.append({"seq": self._seq, "kind": kind, "data": _jsonable(payload)})
            if len(self.events) > 1200:
                dropped = self.events[:600]
                self._dropped_below = dropped[-1]["seq"]
                del self.events[:600]

    def poll(self, since: int = 0) -> dict:
        with self._lock:
            events = [e for e in self.events if e["seq"] > int(since or 0)]
            seq = self._seq
            # 只有"确实把用户还没取走的事件裁掉了"才提示。
            # 注意不能用 events[0].seq > since + 1 判断：level 事件会原地合并、seq 跳号，
            # 那样每次都会误报"记录已滚出缓存"。
            gap = int(since or 0) > 0 and int(since or 0) < self._dropped_below
        return {"seq": seq, "events": events, "state": self.state(), "gap": gap}

    def state(self) -> dict:
        m = self._asst.meeting
        return {
            "listening": bool(self._asst.listening),
            "meeting": ({"start": m["start"].isoformat(), "topic": m.get("topic", "")}
                        if m else None),
            "asr": {"status": self._asst.transcriber.status,
                    "text": self._asst.transcriber.status_text,
                    "ready": self._asst.transcriber.ready},
            "audio": {"ok": not self._asst.audio_error,
                      "msg": self._asst.audio_error or ""},
            "busy": self._asst.busy,
            "count": self.count_today,
            "llm_ready": config.llm_ready(),
            "model": config.get("asr_model", "sensevoice"),
            "today": date.today().strftime("%Y-%m-%d"),
        }

    # ------------------------------------------------------------ 操作
    def toggle_listen(self) -> dict:
        if self._asst.listening:
            self._asst.stop_listening()
        else:
            self._asst.start_listening()
        return self.state()

    def toggle_meeting(self) -> dict:
        if self._asst.meeting:
            self._asst.end_meeting(auto_summary=True)
        else:
            self._asst.start_meeting()
        return self.state()

    def make_daily(self, day: str | None = None) -> dict:
        ok = self._asst.generate_daily(day=day or date.today().strftime("%Y-%m-%d"),
                                       silent_if_empty=False)
        return {"ok": ok}

    def get_settings(self) -> dict:
        return {
            "mic_enabled": config.get("mic_enabled", True),
            "system_audio": config.get("system_audio", True),
            "mic_device_id": config.get("mic_device_id"),
            "asr_model": config.get("asr_model", "sensevoice"),
            "asr_language": config.get("asr_language", "auto"),
            "daily_report_enabled": config.get("daily_report_enabled", True),
            "daily_report_time": config.get("daily_report_time", "18:30"),
            "always_on_top": config.get("always_on_top", False),
            "llm": config.llm,
        }

    def save_settings(self, patch: dict) -> dict:
        allowed = {
            "mic_enabled", "system_audio", "mic_device_id", "asr_model",
            "asr_language", "daily_report_enabled", "daily_report_time",
            "always_on_top", "llm",
        }
        clean = {k: v for k, v in (patch or {}).items() if k in allowed}

        if "daily_report_time" in clean:
            normalized = parse_hhmm(clean["daily_report_time"])
            if normalized is None:
                return {"ok": False,
                        "msg": "日报时间格式不对，请按 18:30 这样填写"}
            clean["daily_report_time"] = normalized

        before = {k: config.get(k) for k in _RESTART_KEYS}
        config.update(clean)

        if self._window is not None:
            try:
                self._window.on_top = bool(config.get("always_on_top", False))
            except Exception as e:
                log_exc("bridge 设置窗口置顶失败", e)

        changed = [k for k in _RESTART_KEYS
                   if k in clean and before.get(k) != config.get(k)]
        restarted = False
        if changed and self._asst.listening:
            self._asst.restart_listening()
            restarted = True
        msg = "设置已保存" + ("，音频已按新设置重启" if restarted else "")
        return {"ok": True, "msg": msg, "restarted": restarted, "state": self.state()}

    def get_recent(self, n: int = 40) -> list[dict]:
        recs = store.load_segments()
        out = []
        for r in recs[-int(n or 40):]:
            out.append({
                "ts": r.get("ts", 0),
                "text": r.get("text", ""),
                "scope": r.get("scope", "daily"),
            })
        return out

    def get_devices(self) -> dict:
        d = list_devices()
        return {
            "mic": [{"id": i, "name": n} for i, n in d["mic"]],
            "loopback": [{"id": i, "name": n} for i, n in d["loopback"]],
        }

    def test_llm(self, cfg: dict) -> dict:
        """只测传入的配置，绝不改写/落盘 settings.json。"""
        saved = config.llm
        merged = {
            "base_url": (cfg or {}).get("base_url") or saved.get("base_url"),
            "api_key": (cfg or {}).get("api_key") or saved.get("api_key"),
            "model": (cfg or {}).get("model") or saved.get("model"),
            "timeout": min(int(saved.get("timeout", 180)), 45),   # 测试别让人干等三分钟
        }
        try:
            llm.test_connection(merged)
            return {"ok": True, "msg": "连接成功"}
        except Exception as e:
            return {"ok": False, "msg": str(e)[:120]}

    def list_reports(self) -> list[dict]:
        out = []
        for p in store.list_reports():
            try:
                st = p.stat()
                out.append({
                    "path": str(p),
                    "name": p.stem,
                    "kind": "meeting" if p.name.startswith("会议") else "daily",
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "size": st.st_size,
                })
            except Exception as e:
                log_exc(f"bridge.list_reports 跳过 {p.name}", e)
                continue
        return out

    def read_report(self, path: str) -> dict:
        try:
            p = Path(path).resolve()
            if not _within(p, REPORT_DIR):
                return {"ok": False, "text": "路径不合法"}
            return {"ok": True, "text": p.read_text(encoding="utf-8")}
        except Exception as e:
            return {"ok": False, "text": f"读取失败：{e}"}

    def open_path(self, path: str) -> dict:
        """只允许打开 data 目录下的文件，避免页面被注入后能启动任意可执行文件。"""
        try:
            p = Path(path).resolve()
            if not _within(p, DATA_DIR):
                return {"ok": False, "msg": "路径不合法"}
            if not p.exists():
                return {"ok": False, "msg": "文件不存在"}
            os.startfile(str(p))  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def open_reports_dir(self) -> dict:
        return self.open_path(str(REPORT_DIR))

    def js_error(self, msg: str) -> dict:
        """前端异常落到日志，避免界面出错时完全没有痕迹。"""
        log_exc("frontend", str(msg)[:500])
        try:
            p = LOG_DIR / "frontend.log"
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        except Exception:
            pass
        return {"ok": True}

    def show_message(self, text: str) -> None:
        self._push("notice", text)

    # ------------------------------------------------------------ 窗口
    def win_pos(self) -> dict:
        if not self._window:
            return {"x": 0, "y": 0}
        try:
            return {"x": int(self._window.x), "y": int(self._window.y)}
        except Exception:
            return {"x": 0, "y": 0}

    def win_move(self, x: int, y: int) -> dict:
        if self._window:
            try:
                self._window.move(int(x), int(y))
            except Exception as e:
                log_exc("bridge.win_move 失败", e)
        return {"ok": True}

    def minimize(self) -> None:
        if self._window:
            try:
                self._window.minimize()
            except Exception as e:
                log_exc("bridge.minimize 失败", e)

    def quit(self) -> None:
        self._closing = True
        try:
            self._asst.shutdown()
        except Exception as e:
            log_exc("bridge.quit shutdown 失败", e)
        if self._window:
            try:
                self._window.destroy()
            except Exception as e:
                log_exc("bridge.quit destroy 失败", e)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            self._asst.shutdown()
        except Exception as e:
            log_exc("bridge.shutdown 失败", e)

    # ------------------------------------------------------------ 定时
    def _scheduler(self) -> None:
        last_check = 0.0
        checked_yesterday = False
        while not self._closing:
            time.sleep(20)
            now = time.time()
            try:
                if not checked_yesterday:
                    checked_yesterday = True
                    self._check_yesterday()
                if now - last_check < 60:
                    continue
                last_check = now
                self._check_daily_time()
            except Exception as e:
                log_exc("bridge._scheduler 异常", e)
                continue

    def _check_yesterday(self) -> None:
        if not config.get("auto_report_on_start", True):
            return
        y = store.yesterday()
        if store.has_daily_report(y):
            return
        recs = [r for r in store.load_segments(y) if r.get("scope") != "meeting"]
        if len(recs) < 3:
            return
        self._push("notice", f"正在补生成 {y} 的日报…")
        self._asst.generate_daily(day=y)

    def _check_daily_time(self) -> None:
        if not config.get("daily_report_enabled", True):
            return
        target_txt = parse_hhmm(config.get("daily_report_time", "18:30"))
        if target_txt is None:            # 老版本存进去的脏数据，别因此把调度搞坏
            return
        target = tuple(int(x) for x in target_txt.split(":"))
        now = datetime.now()
        if (now.hour, now.minute) < target:
            return
        today = now.strftime("%Y-%m-%d")
        if store.has_daily_report(today) or self._asst.busy:
            return
        # 失败退避：以前失败后 has_daily_report 仍为 False，于是每分钟重试一次，白烧额度
        failed_at = self._daily_fail_at.get(today, 0)
        if failed_at and time.time() - failed_at < _DAILY_RETRY_BACKOFF:
            return
        if len(store.load_segments(today)) < 3:
            return
        self._push("notice", "到点了，正在生成今日日报…")
        self._asst.generate_daily(day=today)
