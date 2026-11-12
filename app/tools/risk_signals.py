# -*- coding: utf-8 -*-
"""风控信号与熔断冷却：把采集过程中出现的危险信号结构化落盘，
让编排器能够据此「主动收手、冷却后再用」，而不是在风控窗口里反复硬撞。

两类持久化：
1. data/risk_events.jsonl  —— 每次危险信号追加一行（子进程写，整体容错）；
2. data/risk_cooldown.json —— 平台冷却状态（编排器读写，原子写）。

信号分级：captcha（人机验证）、security_limit（账号安全限制）属高危，
达到阈值即触发冷却；rate_limit（限流）、login_wall（登录墙）、warning（其他告警）
作为辅助信号参与统计与提示。
"""
import json
import os
import time
from typing import Dict, List, Optional

ROOT = os.getcwd()
EVENT_FILE = os.path.join(ROOT, "data", "risk_events.jsonl")
COOLDOWN_FILE = os.path.join(ROOT, "data", "risk_cooldown.json")

# 信号类型
KIND_CAPTCHA = "captcha"
KIND_RATE_LIMIT = "rate_limit"
KIND_SECURITY = "security_limit"
KIND_LOGIN_WALL = "login_wall"
KIND_WARNING = "warning"

# 触发冷却的高危信号（限流也视为平台已介入，账号被警告背景下宁严勿松）
HIGH_RISK_KINDS = (KIND_CAPTCHA, KIND_SECURITY, KIND_RATE_LIMIT)

# 阶梯冷却时长（秒）：24 小时内首次 30 分钟、再犯 2 小时、三犯及以上 24 小时。
# 参考社区保守型风控闸门（CreatorHub 等）：风控窗口内绝不硬撞，冷却随再犯升级。
COOLDOWN_TIERS = (1800, 7200, 86400)

# 兼容旧引用：默认冷却区间取首档
COOLDOWN_MIN = COOLDOWN_TIERS[0]
COOLDOWN_MAX = COOLDOWN_TIERS[0]

# 单轮高危信号达到该次数即触发冷却（首次信号即收手）
HIGH_RISK_THRESHOLD = 1


def record(platform: str, kind: str, detail: str = "",
           task_id: Optional[str] = None) -> None:
    """记录一条风控信号。整体容错，绝不影响采集主流程。"""
    try:
        event = {
            "ts": round(time.time(), 3),
            "platform": platform,
            "kind": kind,
            "detail": str(detail)[:200],
            "task_id": task_id,
        }
        os.makedirs(os.path.dirname(EVENT_FILE), exist_ok=True)
        with open(EVENT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _read_all_events() -> List[Dict]:
    if not os.path.exists(EVENT_FILE):
        return []
    out: List[Dict] = []
    try:
        with open(EVENT_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        out.append(ev)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def events_since(platform: str, since_ts: float,
                 kinds: Optional[tuple] = None) -> List[Dict]:
    """读取某平台在 since_ts 之后的（指定类型）信号。"""
    result = []
    for ev in _read_all_events():
        if ev.get("platform") != platform:
            continue
        if float(ev.get("ts", 0)) < since_ts:
            continue
        if kinds is not None and ev.get("kind") not in kinds:
            continue
        result.append(ev)
    return result


def count_high_risk(platform: str, since_ts: float) -> int:
    """统计某平台在 since_ts 之后的高危信号次数。"""
    return len(events_since(platform, since_ts, HIGH_RISK_KINDS))


def _read_cooldown() -> Dict[str, Dict]:
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cooldown(data: Dict[str, Dict]) -> None:
    os.makedirs(os.path.dirname(COOLDOWN_FILE), exist_ok=True)
    tmp = COOLDOWN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, COOLDOWN_FILE)


def in_cooldown(platform: str) -> bool:
    """平台当前是否处于冷却中。过期视为已解除（保留再犯历史用于阶梯升级）。"""
    data = _read_cooldown()
    item = data.get(platform)
    if not item:
        return False
    return time.time() < float(item.get("until_ts", 0))


def cooldown_remaining(platform: str) -> int:
    """剩余冷却秒数（未冷却返回 0）。"""
    data = _read_cooldown()
    item = data.get(platform)
    if not item:
        return 0
    return max(0, int(float(item.get("until_ts", 0)) - time.time()))


def cooldown_reason(platform: str) -> str:
    item = _read_cooldown().get(platform)
    return item.get("reason", "") if item else ""


def set_cooldown(platform: str, seconds: int, reason: str = "") -> None:
    """设置平台冷却。"""
    data = _read_cooldown()
    data[platform] = {
        "until_ts": round(time.time() + max(1, int(seconds)), 1),
        "reason": str(reason)[:200],
        "updated_at": round(time.time(), 1),
    }
    _write_cooldown(data)


def escalating_cooldown(platform: str, reason: str = "") -> int:
    """阶梯冷却：24 小时内按再犯次数升级（30 分钟 → 2 小时 → 24 小时）。

    返回本次冷却秒数。再犯历史随冷却状态持久化，过期不清除历史，
    保证「冷却结束又犯」不会重置回最短档。
    """
    data = _read_cooldown()
    item = data.get(platform, {})
    now = time.time()
    history = [t for t in item.get("history", []) if now - float(t) < 86400]
    tier = min(len(history), len(COOLDOWN_TIERS) - 1)
    seconds = COOLDOWN_TIERS[tier]
    history.append(round(now, 1))
    data[platform] = {
        "until_ts": round(now + seconds, 1),
        "reason": str(reason)[:200],
        "updated_at": round(now, 1),
        "history": history,
    }
    _write_cooldown(data)
    return seconds


def clear_cooldown(platform: str) -> None:
    """手动解除平台冷却。"""
    try:
        data = _read_cooldown()
        if platform in data:
            data.pop(platform, None)
            _write_cooldown(data)
    except Exception:
        pass
