# -*- coding: utf-8 -*-
"""profile 环境指纹的稳定分配：为每个平台（账号）在首次创建时分配一套
固定的视口/分辨率组合并持久化，之后长期保持不变。

设计要点（跨层一致性优先）：
- 不同账号之间视口有差异，避免多账号指纹雷同；
- 单个账号内部每次启动都用同一套，避免「这次 1080p、下次 768p」这类不稳定特征；
- 时区固定为 Asia/Shanghai、语言固定 zh-CN，不做随机——国内平台随机成别的
  时区/语言反而会造成跨层不一致、更可疑。
"""
import json
import os
import random
import time
from typing import Dict

ROOT = os.getcwd()
FP_FILE = os.path.join(ROOT, "data", "profile_fingerprint.json")

# 真实常见桌面分辨率（与真人浏览器尺寸一致，避免冷门尺寸）
COMMON_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1600, "height": 900},
]

# 固定的时区与语言（跨层一致，不随机）
TIMEZONE = "Asia/Shanghai"
LOCALE = "zh-CN"


def _read() -> Dict[str, Dict]:
    if not os.path.exists(FP_FILE):
        return {}
    try:
        with open(FP_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: Dict[str, Dict]) -> None:
    os.makedirs(os.path.dirname(FP_FILE), exist_ok=True)
    tmp = FP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, FP_FILE)


def get_viewport(platform: str) -> Dict[str, int]:
    """返回某平台固定的视口；首次调用时随机分配一套并持久化。"""
    data = _read()
    item = data.get(platform)
    if item and "width" in item and "height" in item:
        return {"width": int(item["width"]), "height": int(item["height"])}
    vp = dict(random.choice(COMMON_VIEWPORTS))
    data[platform] = {"width": vp["width"], "height": vp["height"],
                      "timezone": TIMEZONE, "locale": LOCALE,
                      "assigned_at": round(time.time(), 1)}
    _write(data)
    return vp
