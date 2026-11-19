"""桌面助理 · 语音记录与总结

启动入口：WebView2 窗口 + Python 后端。
"""
from __future__ import annotations

import ctypes
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WEB_DIR = ROOT / "app" / "ui" / "web"

WIDTH, HEIGHT = 468, 712
_DEBUG = "--debug" in sys.argv


def _enable_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _screen_size() -> tuple[int, int]:
    try:
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def main() -> None:
    _enable_dpi()
    import webview

    from app.ui.bridge import Bridge

    bridge = Bridge()
    sw, sh = _screen_size()
    x = max(0, sw - WIDTH - 80)
    y = max(0, (sh - HEIGHT) // 2 - 20)

    from app.config import config

    saved_pos = config.get("window_pos")
    if isinstance(saved_pos, (list, tuple)) and len(saved_pos) == 2:
        try:
            x, y = int(saved_pos[0]), int(saved_pos[1])
        except Exception:
            pass

    window = webview.create_window(
        "桌面助理",
        url=str(WEB_DIR / "index.html"),
        js_api=bridge,
        width=WIDTH,
        height=HEIGHT,
        x=x,
        y=y,
        resizable=False,
        frameless=True,
        easy_drag=False,
        on_top=False,
        background_color="#F4F5F7",
        text_select=True,
        min_size=(WIDTH, HEIGHT),
    )
    bridge.window = window

    def on_closing() -> bool:
        try:
            config.update({"window_pos": [int(window.x), int(window.y)]})
        except Exception:
            pass
        bridge.shutdown()
        return True

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    if bridge_state_top():
        try:
            window.on_top = True
        except Exception:
            pass

    webview.start(debug=True if _DEBUG else False)


def bridge_state_top() -> bool:
    try:
        from app.config import config
        return bool(config.get("always_on_top", False))
    except Exception:
        return False


_MUTEX_HANDLE: list = []


def _already_running() -> bool:
    """命名互斥体保证只跑一个实例，避免重复双击录出两份文件。"""
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "meeting-mate-single-instance")
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                None, "meeting-mate 已经在运行了。\n\n看下屏幕右侧的小窗口。",
                "桌面助理 · 已在运行", 0x40,
            )
            return True
        _MUTEX_HANDLE.append(h)  # 持有引用，进程存活期间不释放
        return False
    except Exception:
        return False


def _report_crash(exc: BaseException) -> None:
    """无控制台启动时，错误只能靠弹窗告诉用户，否则就是"双击没反应"。"""
    tb = traceback.format_exc()
    log = ROOT / "data" / "logs" / "crash.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(tb, encoding="utf-8")
    except Exception:
        log = None

    head = [ln for ln in tb.strip().splitlines() if ln.strip()][-1:]
    msg = f"启动失败：{head[0] if head else exc}\n\n完整日志：\n{log or '（写入失败）'}"
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, "桌面助理 · 启动失败", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        if _already_running():
            sys.exit(0)
        main()
    except Exception as exc:
        _report_crash(exc)
        raise
