# -*- coding: utf-8 -*-
"""Local web console for MediaCrawler: pick platform -> scan QR -> view data -> export Excel."""
import json, glob, os, re, time, subprocess, threading
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from tools import login_state
from tools import task_store, orchestrator, task_exporter
from tools import settings
from tools.scheduler import scheduler
import config

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = DATA / "server_run.log"
QR_STATE = DATA / "qrcode_state.json"
VENV_PY = __import__("sys").executable

PLATFORMS = [
    ("xhs", "小红书"), ("dy", "抖音"), ("wb", "微博"), ("ks", "快手"),
    ("bili", "B站"), ("tieba", "贴吧"), ("zhihu", "知乎"), ("toutiao", "今日头条"),
]
PLAT_NAME = dict(PLATFORMS)
CODES = [c for c, _ in PLATFORMS]
state = {"proc": None, "started_at": None, "lock": threading.Lock(), "meta": {}}
app = FastAPI(title="MediaCrawler Console")


@app.on_event("startup")
def _start_scheduler():
    # 服务启动即开始调度；不做开机自启，服务未运行期间错过的任务不补跑。
    scheduler.start()


@app.on_event("shutdown")
def _stop_scheduler():
    scheduler.shutdown()

def normalize_keywords(raw: str) -> str:
    parts = [p.strip() for p in re.split(r"[,，;；\n\r]+", raw or "") if p.strip()]
    return ",".join(parts)


def normalize_note_urls(raw: str) -> str:
    """归一化帖子链接：按逗号/分号/空白(含换行)拆分，只保留 http 链接，用英文逗号拼接。"""
    parts = [p.strip() for p in re.split(r"[,，;；\s]+", raw or "") if p.strip().startswith("http")]
    return ",".join(parts)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    # 禁缓存：确保每次打开都加载最新前端，避免用户看到旧版卡片
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/platforms")
def platforms():
    return {"platforms": [{"code": c, "name": n} for c, n in PLATFORMS]}


@app.get("/api/settings")
def get_settings():
    return settings.load()


@app.post("/api/settings")
async def update_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return settings.save(body or {})


@app.get("/api/login-state")
def login_state_snapshot():
    """读取缓存的登录态快照（不启动浏览器），供前端展示有效/失效。"""
    return {"platforms": login_state.snapshot()}


@app.post("/api/login-check")
async def login_check(request: Request):
    """实际启动浏览器体检；body 带 platform 则单平台，缺省体检全部。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    platform = body.get("platform")
    if platform:
        if platform not in CODES:
            raise HTTPException(400, "bad platform")
        await login_state.check_platform(platform)
    else:
        await login_state.check_all()
    return {"ok": True, "platforms": login_state.snapshot()}


def _normalize_window(raw_start, raw_end):
    """即时采集时间窗：纯日期 start 补 00:00:00、end 补 23:59:59（含当天全天）。"""
    def norm(s, is_end):
        s = (s or "").strip()
        if not s:
            return ""
        if len(s) == 10:  # YYYY-MM-DD
            return s + (" 23:59:59" if is_end else " 00:00:00")
        return s
    return norm(raw_start, False), norm(raw_end, True)


@app.post("/api/start")
async def start(request: Request):
    body = await request.json()
    plat = body.get("platform")
    mode = body.get("mode", "search")
    if plat not in CODES:
        raise HTTPException(400, "bad platform")
    with state["lock"]:
        p = state["proc"]
        if p and p.poll() is None:
            raise HTTPException(409, "已有采集任务正在运行")
        # 仅小红书平台解析用户ID（小红书号）；其他平台只采集帖子，强制关闭
        red_id_on = bool(body.get("get_red_id", True)) and plat == "xhs"
        crawl_mode = body.get("crawl_mode", "login")
        if crawl_mode not in ("login", "guest"):
            raise HTTPException(400, "bad crawl_mode")
        # 帖子/评论模式：帖子模式关闭评论与子评论，减少请求、保护账号
        want_comments = body.get("data_mode", "content") == "comment"
        comment_flag = "true" if want_comments else "false"
        # 时间窗（发布时间过滤范围）
        win_start, win_end = _normalize_window(body.get("time_start"), body.get("time_end"))
        cmd = [VENV_PY, "main.py", "--platform", plat, "--lt", "qrcode",
               "--crawl_mode", crawl_mode,
               "--get_red_id", "true" if red_id_on else "false",
               "--get_comment", comment_flag,
               "--get_sub_comment", comment_flag,
               "--time_start", win_start,
               "--time_end", win_end]
        if mode == "detail":
            urls = normalize_note_urls(body.get("note_urls"))
            if not urls:
                raise HTTPException(400, "请提供有效的帖子链接")
            cmd += ["--type", "detail", "--specified_id", urls]
            label = urls
        else:
            kw = normalize_keywords(body.get("keywords"))
            cmd += ["--type", "search"]
            if kw:
                cmd += ["--keywords", kw]
            label = kw
        DATA.mkdir(parents=True, exist_ok=True)
        # 清理上一次的二维码状态，避免前端读到旧码
        if QR_STATE.exists():
            QR_STATE.unlink()
        out = open(LOG, "w", encoding="utf-8")
        state["proc"] = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT)
        state["started_at"] = time.time()
        state["meta"] = {"platform": plat, "data_mode": "comment" if want_comments else "content",
                         "crawl_mode": crawl_mode, "login_only": False}
    return {"ok": True, "pid": state["proc"].pid, "platform": plat, "mode": mode,
            "crawl_mode": crawl_mode, "input": label}


@app.post("/api/login")
def login_only(request: Request):
    """独立扫码登录：不启动采集，只拉起浏览器走登录（含扫码），成功后进程自行退出。

    登录路径与真实采集完全同源（main.py --login_only），复用 qrcode 状态轮询与
    /api/status、/api/stop，支撑前端「先登录、再采集」的独立入口。
    """
    import asyncio
    async def _read():
        try:
            return await request.json()
        except Exception:
            return {}
    body = asyncio.run(_read())
    plat = body.get("platform")
    if plat not in CODES:
        raise HTTPException(400, "bad platform")
    with state["lock"]:
        p = state["proc"]
        if p and p.poll() is None:
            raise HTTPException(409, "已有任务正在运行，请先停止")
        # 独立登录始终使用登录身份；login_only 会在登录完成后、进入采集前退出
        cmd = [VENV_PY, "main.py", "--platform", plat, "--lt", "qrcode",
               "--crawl_mode", "login", "--login_only", "true",
               "--get_red_id", "false",
               "--get_comment", "false", "--get_sub_comment", "false"]
        DATA.mkdir(parents=True, exist_ok=True)
        if QR_STATE.exists():
            QR_STATE.unlink()
        out = open(LOG, "w", encoding="utf-8")
        state["proc"] = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT)
        state["started_at"] = time.time()
        state["meta"] = {"platform": plat, "data_mode": "content",
                         "crawl_mode": "login", "login_only": True}
    return {"ok": True, "pid": state["proc"].pid, "platform": plat, "login_only": True}


@app.post("/api/logout")
def logout(request: Request):
    """退出登录/换号：把指定平台的登录目录隔离为备份（不删除任何数据），
    并重置登录态缓存；下次「扫码登录」即全新登录，可登录其他账号。"""
    import asyncio
    async def _read():
        try:
            return await request.json()
        except Exception:
            return {}
    body = asyncio.run(_read())
    plats = body.get("platforms") or ([body["platform"]] if body.get("platform") else [])
    plats = [p for p in plats if p in CODES]
    if not plats:
        raise HTTPException(400, "bad platform")
    with state["lock"]:
        p = state["proc"]
        if p and p.poll() is None:
            raise HTTPException(409, "已有任务正在运行，请先停止再退出登录")
        backed_up = []
        for plat in plats:
            d = ROOT / "browser_data" / (config.USER_DATA_DIR % plat)
            if d.exists():
                bak = d.with_name(d.name + time.strftime("_bak_%Y%m%d%H%M%S"))
                d.rename(bak)
                backed_up.append(plat)
            login_state._merge(plat, {"logged_in": False, "profile_exists": False,
                                      "last_checked": "", "detail": "已退出登录，登录目录已隔离备份"})
        if QR_STATE.exists():
            QR_STATE.unlink()
    return {"ok": True, "logged_out": plats, "profile_backed_up": backed_up,
            "platforms": login_state.snapshot()}


@app.post("/api/stop")
def stop():
    p = state["proc"]
    if QR_STATE.exists():
        QR_STATE.unlink()
    if p and p.poll() is None:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        try:
            p.wait(timeout=8)
        except Exception:
            p.kill()
        return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False}


@app.get("/api/qrcode")
def qrcode():
    """读取子进程写入的二维码状态，供前端内嵌展示。"""
    if QR_STATE.exists():
        try:
            return json.loads(QR_STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"status": "none"}


@app.get("/api/status")
def status():
    # 任务路径（立即执行/定时，走 orchestrator）与即时单进程路径统一收口
    tid = scheduler.active_task_id
    if not tid:
        # 服务重启后内存态丢失：回退到 tasks.json 中仍标记运行的任务，
        # 保证孤儿子进程采集期间进度卡仍可见（仅展示回退，不改调度器状态；
        # 子进程结束后会自行把任务状态写回，回退随之消失）。
        for t in task_store.list_tasks():
            if t.get("status") == task_store.STATUS_RUNNING:
                tid = t["id"]
                break
    task = task_store.get_task(tid) if tid else None
    p = state["proc"]
    state_running = bool(p and p.poll() is None)
    running = bool(task) or state_running

    log_lines = []
    if task:
        # 任务路径：读当前正在采集平台的编排日志来重建里程碑
        cur_plat = task.get("current_platform") or (
            task["platforms"][0] if task.get("platforms") else "")
        meta = {"platform": cur_plat, "data_mode": task.get("data_mode", "content"),
                "crawl_mode": task.get("crawl_mode", "login"), "login_only": False}
        lf = DATA / f"orchestrator_{tid}_{cur_plat}.log"
        if lf.exists():
            # 全量读取用于计数（已查看/已过滤/失败按整轮累计，截断会少计
            # 导致漏斗等式不成立）；展示用的 tail 仍只取最近 40 行。
            log_lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
    else:
        # 即时单进程路径
        if LOG.exists():
            log_lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        meta = state.get("meta", {}) or {}

    prog = None
    plat_code = meta.get("platform")
    if plat_code and log_lines:
        from tools import progress
        prog = progress.interpret(
            log_lines,
            PLAT_NAME.get(plat_code, plat_code),
            data_mode=meta.get("data_mode", "content"),
            crawl_mode=meta.get("crawl_mode", "login"),
            is_login_only=meta.get("login_only", False),
        )
    # 实时采集计数：以 jsonl 落盘行为准（全平台统一），按本轮 started_ts 过滤，
    # 供进度卡展示「已采 N 条」并随轮询增长。
    live = None
    if task and plat_code:
        started_ts = (task.get("platform_status", {}).get(plat_code, {}) or {}).get("started_ts")
        if started_ts:
            from tools.live_count import live_counts
            live = live_counts(plat_code, started_ts, meta.get("data_mode", "content"))
    # 统一计数口径：进度时间线的 saved/comment 以 jsonl 落盘计数为准。
    # 日志解析只覆盖部分平台，落盘行与导出同源，全平台准确。
    if live and prog:
        prog["counts"]["saved"] = live["saved"]
        prog["counts"]["comment"] = live["comment"]
        for m in prog.get("timeline", []):
            if m.get("key") == "collect":
                m.setdefault("counts", {})["saved"] = live["saved"]
                if str(m.get("text", "")).startswith("已采集"):
                    m["text"] = "已采集 %d 条帖子" % live["saved"]
            elif m.get("key") == "comment":
                m.setdefault("counts", {})["comment"] = live["comment"]
                if str(m.get("text", "")).startswith("已采集"):
                    m["text"] = "已采集 %d 条评论" % live["comment"]
    return {"running": running, "exit_code": None, "tail": log_lines[-40:],
            "progress": prog, "live_counts": live, "task_id": tid or "",
            "task_name": task["name"] if task else ""}


# ==================== 定时任务 API ====================
@app.get("/api/tasks")
def get_tasks():
    # 最新创建的任务置顶（created_at 为 ISO 本地时间串，字典序即时间序）
    tasks = sorted(task_store.list_tasks(),
                   key=lambda t: t.get("created_at", ""), reverse=True)
    return {"tasks": tasks, "active": scheduler.active_task_id}


@app.post("/api/tasks")
async def create_task_api(request: Request):
    payload = await request.json()
    try:
        task = task_store.create_task(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 立即执行任务：创建成功后马上触发；同一时刻只允许一个任务运行
    if task["schedule_type"] == task_store.SCHED_IMMEDIATE:
        if scheduler.active_task_id is not None:
            task_store.update_task(task["id"],
                                   {"message": "已创建，当前有任务在执行，请稍后点立即执行"})
        else:
            threading.Thread(target=_manual_worker, args=(task,), daemon=True).start()
    return task


@app.get("/api/tasks/{tid}")
def get_task_api(tid: str):
    t = task_store.get_task(tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return t


@app.put("/api/tasks/{tid}")
async def update_task_api(tid: str, request: Request):
    changes = await request.json()
    try:
        return task_store.update_task(tid, changes)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.delete("/api/tasks/{tid}")
def delete_task_api(tid: str):
    t = task_store.get_task(tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if tid == scheduler.active_task_id or t.get("status") == task_store.STATUS_RUNNING:
        raise HTTPException(400, "任务正在运行，请先停止再删除")
    try:
        task_store.delete_task(tid)
        return {"ok": True}
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/tasks/{tid}/stop")
def stop_task_api(tid: str):
    if task_store.get_task(tid) is None:
        raise HTTPException(404, "任务不存在")
    orchestrator.request_stop(tid)
    if scheduler.active_task_id != tid:
        # 服务重启后监控循环不在本进程（孤儿采集进程）：按 PID 文件兜底停止
        if orchestrator.stop_orphan(tid):
            return {"ok": True, "message": "已停止"}
    return {"ok": True, "message": "已发送停止信号"}


@app.post("/api/tasks/{tid}/run")
def run_task_api(tid: str):
    t = task_store.get_task(tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if scheduler.active_task_id is not None:
        raise HTTPException(400, "当前有任务正在执行，请稍后再试")
    threading.Thread(target=_manual_worker, args=(t,), daemon=True).start()
    return {"ok": True, "message": "已开始手动执行"}


def _manual_worker(t: dict):
    from datetime import datetime
    from tools import time_util
    tid = t["id"]
    scheduler._active_task_id = tid
    started_at = time_util.get_current_time()
    try:
        task_store.update_task(tid, {"status": task_store.STATUS_RUNNING,
                                     "current_platform": "", "message": "手动执行中"})
        # 手动执行窗口：每日任务现场重算（当天/近N小时滚动），单次/立即用创建时固定的绝对窗口。
        # 手动执行不设"到点 deadline"：所有平台跑完即止；窗口终点仅用于发布时间过滤。
        win = task_store.compute_window(t, datetime.now())
        exec_out = scheduler.execute_once(t, win["win_start"], win["win_end"], None)
        if exec_out["blocked"]:
            final_status = task_store.STATUS_FAILED
        elif exec_out["stopped"]:
            final_status = task_store.STATUS_STOPPED
        else:
            final_status = exec_out["status"]

        # 与定时守护路径一致：写运行历史、清当前平台、记录导出文件
        cur = task_store.get_task(tid)
        history = cur.get("run_history", [])
        history.append({
            "run_date": datetime.now().strftime("%Y-%m-%d"),
            "started_at": started_at,
            "ended_at": time_util.get_current_time(),
            "status": final_status,
            "export_file": exec_out.get("export_path", ""),
        })
        export_files = cur.get("export_files", [])
        if exec_out.get("export_path"):
            export_files.append(exec_out["export_path"])

        # 每日任务跑完回到待运行，等待下一天；单次任务保持最终状态
        if cur["schedule_type"] == task_store.SCHED_DAILY \
                and final_status != task_store.STATUS_STOPPED:
            final_status = task_store.STATUS_PENDING
        if exec_out["stopped"]:
            msg = "已停止"
        elif exec_out["blocked"]:
            msg = "登录态失效，本次未采集"
        else:
            msg = "手动执行完成"
        skipped = exec_out.get("skipped") or []
        if skipped and not exec_out["blocked"]:
            from tools import login_state
            snames = [login_state.PLATFORM_LOGIN_INFO[p]["name"] for p in skipped]
            msg += f"（{'、'.join(snames)}登录态失效已跳过）"
        task_store.update_task(tid, {
            "status": final_status,
            "run_history": history,
            "export_files": export_files,
            "current_platform": "",
            # 写回本轮真实窗口：卡片显示与「导出」按钮跟上最新一轮（每日任务随日期滚动）
            "time_start": win["win_start"],
            "time_end": win["win_end"],
            "message": msg,
        })
    except Exception as e:
        task_store.update_task(tid, {"status": task_store.STATUS_FAILED,
                                     "message": f"执行失败：{e}"})
    finally:
        scheduler._active_task_id = None


def time_util_isots(iso):
    from tools import time_util
    return time_util.iso_to_ts(iso)


@app.get("/api/tasks/{tid}/export")
def export_task_api(tid: str):
    t = task_store.get_task(tid)
    if t is None:
        raise HTTPException(404, "任务不存在")
    try:
        xlsx = task_exporter.export_task(tid, t["time_start"], t["time_end"])
    except Exception as e:
        raise HTTPException(400, f"导出失败：{e}")
    return FileResponse(xlsx, filename=os.path.basename(xlsx))


@app.get("/api/tasks/{tid}/logs")
def task_logs_api(tid: str, platform: str = ""):
    if platform:
        log = DATA / f"orchestrator_{tid}_{platform}.log"
        files = [log] if log.exists() else []
    else:
        files = sorted(glob.glob(str(DATA / f"orchestrator_{tid}_*.log")),
                       key=os.path.getmtime)
    text = ""
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                text += f"===== {os.path.basename(f)} =====\n" + fh.read()[-6000:] + "\n"
        except OSError:
            pass
    return {"logs": text}


@app.get("/api/tasks/file/{encoded_path:path}")
def task_file_api(encoded_path: str):
    from urllib.parse import unquote
    raw = unquote(encoded_path)
    target = Path(raw).resolve()
    data_root = DATA.resolve()
    # 路径安全：仅允许下载 data 目录内的文件，防止路径穿越
    if data_root not in target.parents or not target.is_file():
        raise HTTPException(404, "文件不存在或不可访问")
    return FileResponse(target, filename=target.name)
