# -*- coding: utf-8 -*-
"""扫码登录二维码状态文件：子进程写入，server 读取，前端轮询内嵌展示。

子进程（main.py 各平台 login）把当前二维码 src 写到固定文件，
过期自动刷新时原地覆盖；登录成功写 success。避免系统弹窗。
"""
import json
import os
import time

STATE_FILE = os.path.join(os.getcwd(), "data", "qrcode_state.json")

# 状态：waiting 等待扫码 / success 登录成功 / timeout 等待超时
STATUS_WAITING = "waiting"
STATUS_SUCCESS = "success"
STATUS_TIMEOUT = "timeout"


def write_qr_state(platform: str, status: str, src: str = "") -> None:
    payload = {
        "platform": platform,
        "status": status,
        "src": src,
        "updated_at": int(time.time()),
    }
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def read_qr_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def clear_qr_state() -> None:
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass
