# -*- coding: utf-8 -*-
# 多平台编排器：把一个任务的一轮采集，拆成「逐平台串行执行」。
# 设计要点：单并发（一次只跑一个平台）、平台间充分休息、可随时响应停止信号、
# 每平台进度实时写回 task_store；时间窗由调度器计算后显式传入，本模块不关心调度模式。
import os
import sys
import time
import random
import subprocess
from typing import Dict, Optional, Tuple

from tools import task_store, time_util
from tools import risk_signals
from tools import task_exporter
from tools.live_count import live_counts

ROOT = os.getcwd()
PY = sys.executable

# 停止信号：task_id -> True
_stop_flags: Dict[str, bool] = {}

# 平台间休息区间（秒），降低不同平台行为被关联的风险
PLAT_REST_MIN = 60
PLAT_REST_MAX = 150

# 连续平台普通失败（非风控）达到该次数，提前终止本轮（系统级故障兜底）
CONSECUTIVE_FAIL_LIMIT = 2


def request_stop(task_id: str) -> None:
    _stop_flags[task_id] = True


def clear_stop(task_id: str) -> None:
    _stop_flags.pop(task_id, None)


def _is_stop(task_id: str) -> bool:
    return _stop_flags.get(task_id, False)


def _kill_tree(proc: subprocess.Popen) -> None:
    """结束进程树（Windows 下 taskkill /T）。精简 PATH 时用完整路径。"""
    taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
    try:
        subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    except Exception:
        proc.kill()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()


def build_cmd(task: Dict, platform: str, win_start: str, win_end: str) -> Optional[list]:
    """构建单个平台的 main.py 命令行。"""
    crawl_mode = task["crawl_mode"]
    red_id = "true" if (task.get("get_red_id") and platform == "xhs") else "false"
    want_comments = task["data_mode"] == task_store.DATA_COMMENT
    comments_flag = "true" if want_comments else "false"

    cmd = [
        PY, "main.py", "--platform", platform, "--lt", "qrcode",
        "--crawl_mode", crawl_mode,
        "--get_red_id", red_id,
        "--get_comment", comments_flag,
        "--get_sub_comment", comments_flag,
        "--time_start", win_start or "",
        "--time_end", win_end or "",
    ]

    if task["input_type"] == task_store.INPUT_KEYWORD:
        cmd += ["--type", "search", "--keywords", task["keywords"]]
    else:
        nu = task.get("note_urls", {}) or {}
        # 兼容按平台链接，或统一存在 _all 的链接
        urls = (nu.get(platform) or nu.get("_all") or "").strip()
        if not urls:
            return None  # 该平台没有链接，跳过
        cmd += ["--type", "detail", "--specified_id", urls]
    return cmd


def run_platform(task: Dict, platform: str, win_start: str, win_end: str) -> Dict:
    """执行单个平台并返回结果摘要。"""
    task_id = task["id"]
    cmd = build_cmd(task, platform, win_start, win_end)
    if cmd is None:
        return {"status": "skipped", "contents": 0, "comments": 0, "message": "该平台无链接，跳过"}

    run_start_ts = time.time()
    task_store.update_platform_status(task_id, platform,
                                      {"status": "running", "message": "采集中",
                                       "started_ts": run_start_ts})

    log_path = os.path.join(ROOT, "data", f"orchestrator_{task_id}_{platform}.log")
    out = open(log_path, "a", encoding="utf-8")
    out.write(f"\n===== {time_util.get_current_time()} platform={platform} =====\n")
    out.flush()
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT)
    out.close()

    data_mode = task.get("data_mode", "content")
    last_count_ts = 0.0
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        if _is_stop(task_id):
            _kill_tree(proc)
            live = live_counts(platform, run_start_ts, data_mode)
            return {"status": "stopped", "contents": live["saved"],
                    "comments": live["comment"], "message": "已手动停止"}
        # 运行中每 10 秒把落盘计数写回任务卡（与进度卡大数字同源），
        # 避免任务列表在采集期间一直显示 0
        now = time.time()
        if now - last_count_ts >= 10:
            last_count_ts = now
            live = live_counts(platform, run_start_ts, data_mode)
            task_store.update_platform_status(
                task_id, platform,
                {"contents": live["saved"], "comments": live["comment"]})
        time.sleep(2)

    # 本任务在该平台、本轮窗口内、关键词下、去重后的总条数（与导出口径一致）
    total_c, total_m = task_exporter.count_task_rows(task, platform, win_start, win_end)
    # 统计本轮高危风控信号（验证码/账号安全限制），达到阈值即触发冷却
    high_risk = risk_signals.count_high_risk(platform, run_start_ts)
    if high_risk >= risk_signals.HIGH_RISK_THRESHOLD or (rc != 0 and high_risk > 0):
        cooldown = risk_signals.escalating_cooldown(
            platform, f"本轮高危信号 {high_risk} 次，退出码 {rc}")
        status = "blocked"
        dur = f"{cooldown // 3600} 小时" if cooldown >= 3600 else f"{cooldown // 60} 分钟"
        msg = f"检测到 {high_risk} 次风控信号，已自动冷却 {dur} 以保护账号（24小时内再犯升级）"
    elif rc == 0:
        status = "done"
        if task["data_mode"] == task_store.DATA_COMMENT:
            msg = f"完成，共 {total_m} 条评论"
        else:
            msg = f"完成，共 {total_c} 条帖子"
    else:
        status, msg = "failed", f"异常退出（码 {rc}），详见日志"
    return {"status": status, "contents": total_c, "comments": total_m, "message": msg}


def run_once(task: Dict, win_start: str, win_end: str, deadline_ts: Optional[int] = None,
             platforms: Optional[list] = None) -> Dict:
    """执行一轮：逐平台串行，平台间休息。返回本轮整体结果。

    deadline_ts：正常到点收尾的截止时间戳。到达后不再启动新平台，当前平台跑完即结束；
    与用户手动停止（_stop_flags，立即终止当前平台）相区别。
    platforms：本轮实际执行的平台子集（登录体检失效的平台已被剔除）；缺省为任务全部平台。
    """
    task_id = task["id"]
    results: Dict[str, Dict] = {}
    platforms = platforms if platforms is not None else task["platforms"]
    total = len(platforms)

    consecutive_fail = 0
    for idx, platform in enumerate(platforms):
        if _is_stop(task_id):
            break
        # 已到截止时间：不再启动新平台，直接收尾
        if deadline_ts is not None and time.time() >= deadline_ts:
            task_store.update_task(task_id, {"message": "已到结束时间，收尾并导出"})
            break
        # 风控冷却中：不启动该平台，直接跳过以保护账号
        if risk_signals.in_cooldown(platform):
            remain = risk_signals.cooldown_remaining(platform)
            res = {
                "status": "skipped",
                "contents": 0,
                "comments": 0,
                "message": f"风控冷却中，剩余约 {max(1, remain // 60)} 分钟，已跳过",
            }
            results[platform] = res
            task_store.update_platform_status(task_id, platform, res)
            continue
        task_store.update_task(task_id, {"current_platform": platform})
        res = run_platform(task, platform, win_start, win_end)
        results[platform] = res
        task_store.update_platform_status(task_id, platform, res)

        # 连续普通失败（系统级故障）兜底：达到阈值提前终止本轮
        if res["status"] == "failed":
            consecutive_fail += 1
            if consecutive_fail >= CONSECUTIVE_FAIL_LIMIT:
                task_store.update_task(task_id, {"message": "连续平台失败，提前终止本轮"})
                break
        else:
            consecutive_fail = 0

        # 平台间休息（最后一个平台不需要）
        if res["status"] in ("done", "failed") and idx < total - 1 and not _is_stop(task_id):
            rest = random.randint(PLAT_REST_MIN, PLAT_REST_MAX)
            task_store.update_task(task_id, {"message": f"平台间休息 {rest} 秒"})
            waited = 0
            while waited < rest and not _is_stop(task_id):
                if deadline_ts is not None and time.time() >= deadline_ts:
                    break
                time.sleep(2)
                waited += 2

    stopped = _is_stop(task_id)
    return {"stopped": stopped, "results": results, "win_start": win_start, "win_end": win_end}
