# -*- coding: utf-8 -*-
# 工作台运行时设置（data/settings.json）。目前仅浏览器窗口可见开关：
# 默认关闭（静默采集，窗口离屏）；打开后浏览器窗口正常显示在屏幕上。
import json
import os
import tempfile
from typing import Any, Dict

SETTINGS_FILE = os.path.join(os.getcwd(), "data", "settings.json")

DEFAULTS: Dict[str, Any] = {"browser_visible": False}


def load() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update({k: saved[k] for k in DEFAULTS if k in saved})
    except (OSError, json.JSONDecodeError):
        pass
    return data


def save(changes: Dict[str, Any]) -> Dict[str, Any]:
    data = load()
    for k in DEFAULTS:
        if k in changes:
            data[k] = bool(changes[k])
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETTINGS_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return data


def browser_visible() -> bool:
    """「显示浏览器窗口」是否打开。各浏览器启动入口据此决定是否离屏。"""
    return bool(load().get("browser_visible", False))