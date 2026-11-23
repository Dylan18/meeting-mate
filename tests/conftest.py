"""pytest 共享配置：把项目根加进 sys.path，并让日志写到临时目录。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _logs_to_tmp(tmp_path_factory):
    """测试不要往真实的 data/logs/error.log 里写。

    看门狗、任务锁这些用例会故意制造异常和告警，混进用户日志后，
    以后排查"我这边为什么没记录"会被测试噪声误导。
    """
    from app import log as applog

    logger = applog.setup()
    for h in list(logger.handlers):
        logger.removeHandler(h)
    path = tmp_path_factory.mktemp("logs") / "error.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    yield
    handler.close()
    logger.removeHandler(handler)
