# -*- coding: utf-8 -*-
"""采集量异常波动监控：把每次每平台的采集量沉淀为历史基线，
当本次数量「突 0」或「暴增」、显著偏离历史均值时给出告警。

用途：采集量本身是风控与系统健康的间接探针——平时有数据、这次突然为 0，
往往是被限流或登录失效；数量异常暴增则可能是过滤失效或页面结构变化。
样本不足时只记录、不轻易判定，避免误报。
"""
import json
import os
import statistics
import time
from typing import Dict, List, Optional

ROOT = os.getcwd()
METRIC_FILE = os.path.join(ROOT, "data", "collect_metrics.jsonl")

# 历史基线至少需要的样本数（不含本次），低于该数不做异常判定
MIN_BASELINE = 3
# 暴增倍数阈值（相对历史均值）
SURGE_MULTIPLE = 2.5
# 突 0 判定：历史均值需达到该数量，避免在本来就稀疏的关键词上误报
ZERO_BASELINE_MEAN = 2.0


def record_run(task_id: str, platform: str, contents: int, comments: int) -> None:
    """记录一次某平台的采集量。整体容错，不影响主流程。"""
    try:
        row = {
            "ts": round(time.time(), 1),
            "task_id": task_id,
            "platform": platform,
            "contents": max(0, int(contents)),
            "comments": max(0, int(comments)),
        }
        os.makedirs(os.path.dirname(METRIC_FILE), exist_ok=True)
        with open(METRIC_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _history_counts(platform: str) -> List[int]:
    """读取某平台历史 contents 采集量（按时间顺序）。"""
    if not os.path.exists(METRIC_FILE):
        return []
    out: List[int] = []
    try:
        with open(METRIC_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("platform") == platform:
                    out.append(int(row.get("contents", 0)))
    except OSError:
        return []
    return out


def evaluate(platform: str, contents: int) -> Dict:
    """基于历史基线评估本次采集量是否异常。

    返回 {level, message, baseline_count, baseline_mean}；
    level 为 normal 或 warning。
    """
    history = _history_counts(platform)
    base: Dict = {
        "level": "normal",
        "message": "",
        "baseline_count": len(history),
        "baseline_mean": round(statistics.mean(history), 2) if history else 0,
    }
    if len(history) < MIN_BASELINE:
        base["message"] = f"历史样本 {len(history)} 次，暂不做异常判定"
        return base

    mean = statistics.mean(history)
    std = statistics.pstdev(history)

    # 突 0：本次为 0 但历史平均有数据
    if contents == 0 and mean >= ZERO_BASELINE_MEAN:
        base["level"] = "warning"
        base["message"] = f"本次采集为 0，历史均值约 {mean:.1f} 条，疑似被限流或登录失效"
        return base

    # 暴增：超过历史均值的 SURGE_MULTIPLE 倍，且超过 均值+2倍标准差
    surge_line = max(mean * SURGE_MULTIPLE, mean + 2 * std)
    if contents > surge_line:
        base["level"] = "warning"
        base["message"] = (f"本次采集 {contents} 条，显著高于历史均值 {mean:.1f} 条，"
                           "疑似过滤失效或页面结构变化，请核查")
        return base

    base["message"] = f"采集量正常（历史均值 {mean:.1f} 条）"
    return base
