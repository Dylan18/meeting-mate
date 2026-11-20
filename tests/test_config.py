"""配置测试：深合并、坏 JSON 回退、时间解析。"""
from __future__ import annotations

import json

import pytest

from app import config as cfg_mod


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", path)
    return path


def test_missing_file_writes_defaults(tmp_config):
    c = cfg_mod.Config()
    assert tmp_config.exists()
    assert c.get("asr_model") == "sensevoice"


def test_deep_merge_keeps_missing_fields(tmp_config):
    tmp_config.write_text(json.dumps({"llm": {"api_key": "sk-x"}}), encoding="utf-8")
    c = cfg_mod.Config()
    # 旧版本配置里没有 base_url/model，必须从默认值补上，不能被整体替换掉
    assert c.llm["api_key"] == "sk-x"
    assert c.llm["base_url"] == cfg_mod.DEFAULTS["llm"]["base_url"]
    assert c.llm["model"] == cfg_mod.DEFAULTS["llm"]["model"]


def test_corrupt_json_falls_back_and_backs_up(tmp_config):
    tmp_config.write_text("{ not json", encoding="utf-8")
    c = cfg_mod.Config()
    assert c.get("asr_model") == "sensevoice"
    # 原文件要留档，避免用户手写的 key 无声消失
    assert (tmp_config.parent / "settings.bad").exists()


@pytest.mark.parametrize("raw,expected", [
    ("18:30", "18:30"),
    ("9:00", "09:00"),
    ("09:05", "09:05"),
    ("18：30", "18:30"),        # 中文冒号
    ("18:30:15", "18:30"),
    ("", None),
    ("25:99", None),
    ("abc", None),
    (None, None),
])
def test_parse_hhmm(raw, expected):
    assert cfg_mod.parse_hhmm(raw) == expected


def test_update_is_deep_and_persists(tmp_config):
    c = cfg_mod.Config()
    c.update({"llm": {"model": "glm-4-flash"}})
    on_disk = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert on_disk["llm"]["model"] == "glm-4-flash"
    assert on_disk["llm"]["base_url"] == cfg_mod.DEFAULTS["llm"]["base_url"]
