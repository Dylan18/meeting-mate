"""桌面助理 · 语音记录与总结

启动入口：WebView2 窗口 + Python 后端。
"""
from __future__ import annotations

import ctypes
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WEB_DIR = ROOT / "app" / "ui" / "web"

WIDTH, HEIGHT = 468, 712
_DEBUG = "--debug" in sys.argv


def _rotate(path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    """简单轮转：给 fd 重定向用的日志没法挂 Handler，只能开文件前手动挪一次。"""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except Exception:
        pass


def _ensure_stdio() -> None:
    """pythonw.exe 启动时没有控制台，stdout/stderr 是 None。

    WebView2/.NET 初始化会往标准输出写东西，句柄失效会导致窗口创建静默失败
    （进程活着但永远不出现窗口）。这里把 fd 1/2 接到日志文件上，两头都补。
    """
    log = ROOT / "data" / "logs" / "app.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    _rotate(log)
    for fd, name in ((1, "stdout"), (2, "stderr")):
        try:
            # 故意不关：这个句柄要活到进程结束，接管的正是 fd 1/2
            f = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
            os.dup2(f.fileno(), fd)
            stream = getattr(sys, name)
            if stream is None:
                setattr(sys, name, f)
        except Exception:
            pass


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


def _screen_rect() -> tuple[int, int, int, int]:
    """返回虚拟桌面矩形 (x, y, w, h)，多屏时覆盖所有显示器。"""
    try:
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    except Exception:
        return 0, 0, 1920, 1080


def _clamp_pos(x: int, y: int) -> tuple[int, int]:
    """拔掉副屏后，上次保存的坐标可能落在屏幕外，用户只能删配置自救。

    这里保证窗口至少有一小块（标题栏区域）落在虚拟桌面内，能拖得回来。
    """
    vx, vy, vw, vh = _screen_rect()
    x = max(vx - WIDTH + 120, min(x, vx + vw - 120))
    y = max(vy, min(y, vy + vh - 80))
    return x, y


def main() -> None:
    _ensure_stdio()
    _enable_dpi()
    import webview

    from app.log import install_hooks
    from app.ui.bridge import Bridge

    install_hooks()
    bridge = Bridge()
    vx, vy, vw, vh = _screen_rect()
    x = max(0, vx + vw - WIDTH - 80)
    y = max(0, vy + (vh - HEIGHT) // 2 - 20)

    from app.config import config

    saved_pos = config.get("window_pos")
    if isinstance(saved_pos, (list, tuple)) and len(saved_pos) == 2:
        try:
            x, y = _clamp_pos(int(saved_pos[0]), int(saved_pos[1]))
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
    bridge.attach_window(window)

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

    webview.start(debug=bool(_DEBUG))


def bridge_state_top() -> bool:
    try:
        from app.config import config
        return bool(config.get("always_on_top", False))
    except Exception:
        return False


_MUTEX_HANDLE: list = []


def _already_running() -> bool:
    """命名互斥体保证只跑一个实例，避免重复双击录出两份文件。

    注意 use_last_error=True：ctypes 在两次调用之间可能重置线程的 last error，
    直接 `windll.kernel32` + `GetLastError()` 会偶发拿到 0，判成"没在运行"。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.CreateMutexW(None, False, "meeting-mate-single-instance")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
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
