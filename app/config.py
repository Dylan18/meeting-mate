"""配置管理：settings.json 读写，带默认值与容错。"""
from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
REPORT_DIR = DATA_DIR / "reports"
SESSION_DIR = DATA_DIR / "sessions"
MODEL_DIR = APP_DIR / "models"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "settings.json"

for _d in (DATA_DIR, REPORT_DIR, SESSION_DIR, MODEL_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULTS: dict[str, Any] = {
    # 录音
    "mic_enabled": True,
    "mic_device_id": None,          # None = 自动选默认麦克风
    "system_audio": True,           # 系统声音回环（会议对方声音）
    "mic_gain": 1.0,
    "system_gain": 1.0,
    # 语音识别
    "asr_model": "sensevoice",      # sensevoice / paraformer-small
    "asr_language": "auto",         # auto / zh / en
    
    # 大模型
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "timeout": 180,
    },
    # 自动化
    "daily_report_time": "18:30",
    "auto_report_on_start": True,   # 启动时若昨天没日报，自动补一份
    "daily_report_enabled": True,
    # 界面
    "always_on_top": False,
    "start_minimized": False,
    "close_to_tray": True,
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            if CONFIG_PATH.exists():
                try:
                    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                    self._data = _deep_merge(DEFAULTS, raw)
                except Exception:
                    self._data = copy.deepcopy(DEFAULTS)
            else:
                self.save()

    def save(self) -> None:
        with self._lock:
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_PATH)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, patch: dict) -> None:
        with self._lock:
            self._data = _deep_merge(self._data, patch)
            self.save()

    @property
    def llm(self) -> dict:
        with self._lock:
            return dict(self._data.get("llm", {}))

    def llm_ready(self) -> bool:
        cfg = self.llm
        return bool(cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"))


config = Config()
