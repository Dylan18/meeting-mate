"""统一异常日志。

为什么要这个模块：项目里 66 处 except，其中 38 处直接 pass。对于"设备枚举失败""窗口
还没就绪"这类预期内的情况，静默是正确的；但"转写结果没能落盘""音频线程崩了"这类问题
一旦静默，用户丢的是数据，现场却什么都不剩。

约定：
- 预期内、可忽略 → 继续 pass，但别用 log_exc 制造噪音
- 影响用户可见结果的异常 → log_exc("where", e)，写 data/logs/error.log
- 后台线程 / 定时任务里的未捕获异常 → 通过 install_hooks() 统一记录

日志按 2MB × 3 轮转，常驻跑一周也不会把磁盘写满。
"""
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Any

_LOGGER_NAME = "meeting-mate"
_configured = False
_config_lock = threading.Lock()
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 3


def setup() -> logging.Logger:
    """初始化根日志器（幂等）。"""
    global _configured
    with _config_lock:
        logger = logging.getLogger(_LOGGER_NAME)
        if _configured:
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            from .config import LOG_DIR
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                LOG_DIR / "error.log",
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUPS,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(handler)
        except Exception:
            # 连日志都开不了就退化成 stderr，至少不因为日志挂掉整个程序
            logger.addHandler(logging.StreamHandler(sys.stderr))
        _configured = True
        return logger


def get(module: str = "app") -> logging.Logger:
    logger = setup()
    if module == "app":
        return logger
    return logger.getChild(module)


def log_exc(where: str, exc: Any = None, level: int = logging.ERROR) -> None:
    """记录一处被降级处理的异常，附带调用位置，方便 grep 定位。"""
    try:
        get("exc").log(level, "%s: %s", where, exc, exc_info=_has_traceback(exc))
    except Exception:
        pass


def _has_traceback(exc: Any) -> bool:
    return isinstance(exc, BaseException) and exc.__traceback__ is not None


def install_hooks() -> None:
    """后台线程与主线程的未捕获异常都落到日志，避免 pythonw 下无声消失。"""
    logger = get("uncaught")

    def _thread_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        logger.error(
            "线程 %s 未捕获异常",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def _ex_hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.error("主线程未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    try:
        threading.excepthook = _thread_hook
    except Exception:
        pass
    try:
        sys.excepthook = _ex_hook
    except Exception:
        pass
