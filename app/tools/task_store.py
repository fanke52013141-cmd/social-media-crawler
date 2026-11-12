# -*- coding: utf-8 -*-
# 定时舆情采集任务的模型与本地持久化。
# 用单个 JSON 文件（data/tasks.json）保存全部任务，原子写入，简单可靠、便于离线便携。
import os
import re
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ISO_FMT = "%Y-%m-%dT%H:%M:%S"

DATA_DIR = os.path.join(os.getcwd(), "data")
TASK_FILE = os.path.join(DATA_DIR, "tasks.json")

# 任务整体状态
STATUS_PENDING = "pending"      # 已创建，等待到点运行
STATUS_RUNNING = "running"      # 正在执行
STATUS_DONE = "done"            # 单次任务已完成
STATUS_FAILED = "failed"        # 执行失败
STATUS_STOPPED = "stopped"      # 已被手动停止

# 采集对象模式
DATA_CONTENT = "content"        # 帖子/笔记/视频采集
DATA_COMMENT = "comment"        # 评论采集

# 输入方式
INPUT_KEYWORD = "keyword"       # 关键词
INPUT_DETAIL = "detail"         # 多链接

# 调度方式
SCHED_IMMEDIATE = "immediate"   # 立即执行（创建后马上跑）
SCHED_ONCE = "once"             # 单次定时
SCHED_DAILY = "daily"           # 每天重复

# 每日任务的窗口模式
WINDOW_TODAY = "today"          # 当天 00:00:00–23:59:59，随日期自动滚动（默认）
WINDOW_ROLLING = "rolling"      # 执行时刻往前推 window_hours（近 N 小时滚动）

VALID = {
    "data_mode": (DATA_CONTENT, DATA_COMMENT),
    "input_type": (INPUT_KEYWORD, INPUT_DETAIL),
    "crawl_mode": ("login", "guest"),
    "schedule_type": (SCHED_IMMEDIATE, SCHED_ONCE, SCHED_DAILY),
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _read_raw() -> List[Dict[str, Any]]:
    if not os.path.exists(TASK_FILE):
        return []
    try:
        with open(TASK_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_raw(tasks: List[Dict[str, Any]]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TASK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TASK_FILE)


def _new_id() -> str:
    return "T" + time.strftime("%Y%m%d%H%M%S", time.localtime()) + uuid.uuid4().hex[:6]


def compute_window(task: Dict[str, Any], run_dt: datetime) -> Dict[str, Any]:
    """计算本轮执行的发布时间窗口。

    daily：每次触发时现场重算——today 模式取执行日当天 00:00:00–23:59:59（随日期自动滚动），
           rolling 模式按执行时刻回推 window_hours；
    once/immediate：用创建时固定的绝对窗口，空串表示不限时间。

    deadline_ts 是编排器的「到点收尾」截止时间：today 模式取当天终点；rolling 模式不设截止
    （窗口终点就是执行时刻本身，若当作截止会导致一触发就收尾、一个平台都不采）；
    绝对窗口取窗口终点（无终点则不设截止）。
    """
    sched = task.get("schedule_type", SCHED_IMMEDIATE)
    if sched == SCHED_DAILY:
        if task.get("window_mode", WINDOW_TODAY) == WINDOW_TODAY:
            day_start = datetime.combine(run_dt.date(), datetime.min.time())
            day_end = datetime.combine(run_dt.date(), datetime.max.time().replace(microsecond=0))
            return {
                "win_start": day_start.strftime(ISO_FMT),
                "win_end": day_end.strftime(ISO_FMT),
                "deadline_ts": int(time.mktime(day_end.timetuple())),
            }
        hours = int(task.get("window_hours", 24))
        win_start_dt = run_dt - timedelta(hours=hours)
        return {
            "win_start": win_start_dt.strftime(ISO_FMT),
            "win_end": run_dt.strftime(ISO_FMT),
            "deadline_ts": None,
        }
    # once / immediate：创建时固定的绝对窗口
    ts = str(task.get("time_start") or "").strip()
    te = str(task.get("time_end") or "").strip()
    from tools import time_util
    return {"win_start": ts, "win_end": te, "deadline_ts": time_util.iso_to_ts(te)}


def create_task(payload: Dict[str, Any]) -> Dict[str, Any]:
    """根据前端提交的参数创建任务并做基础校验。

    定时任务已收敛为唯一形态：多平台 + 多个关键词 + 帖子采集；
    执行时刻（run_time）与发布时间窗口（window_hours）各自独立可配置。
    """
    platforms = payload.get("platforms") or []
    if not isinstance(platforms, list) or not platforms:
        raise ValueError("请至少选择一个平台")

    crawl_mode = payload.get("crawl_mode", "login")
    if crawl_mode not in ("login", "guest"):
        raise ValueError(f"参数 crawl_mode 非法：{crawl_mode}")

    schedule_type = payload.get("schedule_type", SCHED_IMMEDIATE)
    if schedule_type not in (SCHED_IMMEDIATE, SCHED_ONCE, SCHED_DAILY):
        raise ValueError(f"参数 schedule_type 非法：{schedule_type}")

    # 采集内容与定位方式
    data_mode = payload.get("data_mode", DATA_CONTENT)
    if data_mode not in (DATA_CONTENT, DATA_COMMENT):
        raise ValueError(f"参数 data_mode 非法：{data_mode}")
    # 评论依附具体帖子：评论模式强制链接定位
    if data_mode == DATA_COMMENT:
        input_type = INPUT_DETAIL
    else:
        input_type = payload.get("input_type", INPUT_KEYWORD)
        if input_type not in (INPUT_KEYWORD, INPUT_DETAIL):
            raise ValueError(f"参数 input_type 非法：{input_type}")

    run_time = (payload.get("run_time") or "").strip()
    if schedule_type == SCHED_DAILY:
        if not re.match(r"^\d{2}:\d{2}$", run_time):
            raise ValueError("请设置每天执行时刻（如 09:00）")
        hh, mm = (int(x) for x in run_time.split(":"))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("执行时刻不合法")
    elif schedule_type == SCHED_ONCE:
        if _parse_iso(run_time) is None:
            raise ValueError("请设置单次执行的具体时间")
    # immediate 不需要 run_time

    try:
        window_hours = int(payload.get("window_hours", 24))
    except (TypeError, ValueError):
        raise ValueError("时间窗时长不合法")
    if window_hours <= 0:
        raise ValueError("时间窗时长必须大于 0")

    # 每日任务窗口模式：默认当天（随日期自动滚动），可选近 N 小时滚动
    window_mode = payload.get("window_mode", WINDOW_TODAY)
    if window_mode not in (WINDOW_TODAY, WINDOW_ROLLING):
        raise ValueError(f"参数 window_mode 非法：{window_mode}")

    # 关键词/链接校验
    # 关键词归一化：全角逗号/分号/空白统一为英文逗号分隔，与平台逐词搜索、
    # 导出按 source_keyword 过滤的口径保持一致（修复整串搜索导致 0 结果的问题）
    keywords = ",".join(
        p.strip() for p in re.split(r"[,，;；\s]+", (payload.get("keywords") or "")) if p.strip()
    )
    note_urls_in = payload.get("note_urls")
    if input_type == INPUT_KEYWORD:
        if not keywords:
            raise ValueError("请填写关键词")
        note_urls = {}
    else:
        # 链接：兼容按平台字典，或统一字符串（存到 _all，由编排器回退读取）
        if isinstance(note_urls_in, dict):
            note_urls = {k: str(v).strip() for k, v in note_urls_in.items()}
        else:
            note_urls = {"_all": str(note_urls_in or "").strip()}
        if not any(note_urls.values()):
            raise ValueError("请提供有效的帖子链接")

    get_red_id = bool(payload.get("get_red_id", True)) and "xhs" in platforms \
        and data_mode == DATA_CONTENT

    # 计算展示窗口；daily 真正执行时按触发时刻现场重算（当天/滚动），once/immediate 创建时固定
    now0 = datetime.now()
    if schedule_type == SCHED_DAILY:
        run_today = datetime.combine(now0.date(), datetime.strptime(run_time, "%H:%M").time())
        # 今天执行时刻已过：明天才生效，避免一创建就立即补跑当天
        start_date = (now0.date() + timedelta(days=1)).strftime("%Y-%m-%d") \
            if now0 >= run_today else now0.strftime("%Y-%m-%d")
        next_run = run_today
    elif schedule_type == SCHED_ONCE:
        start_date = ""
        next_run = _parse_iso(run_time)
    else:
        start_date = ""
        next_run = now0
    if schedule_type == SCHED_DAILY:
        # 初始窗口仅作首轮展示：today 取今天 00:00–23:59，rolling 按生效执行时刻回推
        win = compute_window({"schedule_type": SCHED_DAILY, "window_mode": window_mode,
                              "window_hours": window_hours}, next_run)
    else:
        # once/immediate：创建时固定绝对日期区间；未选日期即不限时间（真正的「全部」）
        win = {"win_start": str(payload.get("time_start") or "").strip(),
               "win_end": str(payload.get("time_end") or "").strip(),
               "deadline_ts": None}

    task = {
        "id": _new_id(),
        "name": (payload.get("name") or "舆情采集任务").strip(),
        "platforms": list(platforms),
        "data_mode": data_mode,
        "input_type": input_type,
        "keywords": keywords,
        "note_urls": note_urls,
        "crawl_mode": crawl_mode,
        "get_red_id": get_red_id,
        "schedule_type": schedule_type,
        "start_date": start_date,
        "run_time": run_time,
        "window_hours": window_hours,
        "window_mode": window_mode,
        "time_start": win["win_start"],
        "time_end": win["win_end"],
        "repeat_end": (payload.get("repeat_end") or "").strip(),
        "status": STATUS_PENDING,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "current_platform": "",
        "platform_status": {
            p: {"status": "pending", "contents": 0, "comments": 0, "message": ""}
            for p in platforms
        },
        "run_history": [],
        "message": "",
    }
    tasks = _read_raw()
    tasks.append(task)
    _write_raw(tasks)
    return task


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, ISO_FMT)
    except (TypeError, ValueError):
        return None


def list_tasks() -> List[Dict[str, Any]]:
    return _read_raw()


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    for t in _read_raw():
        if t.get("id") == task_id:
            return t
    return None


def update_task(task_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    """局部更新任务字段并持久化。"""
    tasks = _read_raw()
    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if target is None:
        raise KeyError(f"任务不存在：{task_id}")
    target.update(changes)
    target["updated_at"] = _now_iso()
    _write_raw(tasks)
    return target


def delete_task(task_id: str) -> bool:
    tasks = _read_raw()
    remain = [t for t in tasks if t.get("id") != task_id]
    if len(remain) == len(tasks):
        return False
    _write_raw(remain)
    return True


def update_platform_status(task_id: str, platform: str, status_info: Dict[str, Any]) -> None:
    """更新单个平台的执行进度。"""
    t = get_task(task_id)
    if t is None:
        return
    ps = t.get("platform_status", {})
    cur = ps.get(platform, {"status": "pending", "contents": 0, "comments": 0, "message": ""})
    cur.update(status_info)
    ps[platform] = cur
    update_task(task_id, {"platform_status": ps})
