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


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log = ROOT / "data" / "logs" / "crash.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        raise
