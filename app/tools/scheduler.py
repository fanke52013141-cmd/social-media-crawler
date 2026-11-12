# -*- coding: utf-8 -*-
# 后台调度器：在服务进程内以守护线程运行，周期性检查任务，到点启动一轮采集，
# 收尾后调用导出器生成多 sheet Excel。
# 重要边界：仅在工作台服务运行时才调度；服务未启动期间错过的任务不补跑（不做开机自启）。
import hashlib
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

import config
from tools import task_store, orchestrator, time_util

TICK_SECONDS = 5
ISO_FMT = "%Y-%m-%dT%H:%M:%S"


class Scheduler:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._active_task_id: Optional[str] = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="task-scheduler", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._running = False

    @property
    def active_task_id(self) -> Optional[str]:
        return self._active_task_id

    # ---------- 主循环 ----------
    def _loop(self) -> None:
        while self._running:
            try:
                self._tick()
            except Exception as e:
                print(f"[Scheduler] tick error: {e}")
            time.sleep(TICK_SECONDS)

    def _tick(self) -> None:
        # 同一时刻只执行一个任务，避免浏览器/资源冲突
        if self._active_task_id is not None:
            return
        now = datetime.now()
        for task in task_store.list_tasks():
            if task["status"] not in (task_store.STATUS_PENDING, task_store.STATUS_RUNNING):
                continue
            due = self._check_due(task, now)
            if due is not None:
                self._launch(task, due)
                return

    # ---------- 到期判断 ----------
    def _check_due(self, task: Dict, now: datetime) -> Optional[Dict]:
        """执行时刻（run_time）与窗口（window_hours）解耦：到点后按执行时刻动态回推窗口。"""
        if task["schedule_type"] == task_store.SCHED_IMMEDIATE:
            # 立即执行任务由创建入口直接触发，不参与定时调度，避免被重复执行
            return None
        if task["schedule_type"] == task_store.SCHED_ONCE:
            run_dt = task_store._parse_iso(task["run_time"])
            if run_dt is None:
                return None
            if now < run_dt:
                return None
            win = task_store.compute_window(task, run_dt)
            return {
                "win_start": win["win_start"],
                "win_end": win["win_end"],
                "deadline_ts": win["deadline_ts"],
                "run_date": now.strftime("%Y-%m-%d"),
            }

        # 每日重复
        today_str = now.strftime("%Y-%m-%d")
        # 超过重复截止日期：标记停止，不再执行
        if task.get("repeat_end") and today_str > task["repeat_end"]:
            task_store.update_task(task["id"], {"status": task_store.STATUS_STOPPED,
                                                "message": "已过重复截止日期"})
            return None

        # 未到任务生效日期：新建的每日任务若今天执行时刻已过，从明天才开始，不补跑
        if task.get("start_date") and today_str < task["start_date"]:
            return None

        # 今天已经启动过一轮
        run_dates = [r.get("run_date") for r in task.get("run_history", [])]
        if today_str in run_dates:
            return None

        try:
            run_today = datetime.combine(
                now.date(), datetime.strptime(task["run_time"], "%H:%M").time()
            )
        except ValueError:
            return None
        if now < run_today:
            return None
        win = task_store.compute_window(task, run_today)
        # 每日任务随机抖动：按「任务ID+日期」确定性种子延后 0–N 分钟启动，
        # 消除"每天准点同一时刻"的机器特征；同日多次 tick 抖动值稳定，不漂移。
        # 只延后不提前；窗口仍按原执行时刻回推，语义不变。
        jitter_max = int(getattr(config, "SCHED_JITTER_MAX_MIN", 90))
        if jitter_max > 0:
            seed = int(hashlib.md5(
                f"{task['id']}:{today_str}".encode("utf-8")).hexdigest()[:8], 16)
            if now < run_today + timedelta(minutes=seed % (jitter_max + 1)):
                return None
        return {
            "win_start": win["win_start"],
            "win_end": win["win_end"],
            "deadline_ts": win["deadline_ts"],
            "run_date": today_str,
        }

    # ---------- 启动执行 ----------
    def _preflight_login(self, task: Dict) -> Dict[str, bool]:
        """任务前登录体检：仅检测本任务涉及的登录平台，返回 platform -> 是否有效。

        游客模式直接跳过（零账号、无需登录）；体检在采集启动前完成并关闭浏览器，
        与后续 run_once 错开，不会争抢同一 profile。
        """
        import asyncio as _asyncio
        from tools import login_state

        result: Dict[str, bool] = {}
        if task.get("crawl_mode") != "login":
            return result

        async def _run() -> None:
            for plat in task["platforms"]:
                if plat not in login_state.PLATFORM_LOGIN_INFO:
                    continue
                try:
                    r = await login_state.check_platform(plat)
                    result[plat] = bool(r.get("logged_in"))
                except Exception as e:
                    print(f"[Scheduler] preflight {plat} error: {e}")
                    result[plat] = False

        try:
            _asyncio.run(_run())
        except Exception as e:
            print(f"[Scheduler] preflight error: {e}")
        return result

    def _launch(self, task: Dict, due: Dict) -> None:
        self._active_task_id = task["id"]
        worker = threading.Thread(
            target=self._worker, name=f"task-worker-{task['id']}",
            args=(task["id"], due), daemon=True,
        )
        worker.start()

    def execute_once(self, task: Dict, win_start: str, win_end: str,
                     deadline_ts: Optional[float]) -> Dict:
        """统一的单次执行核心：任务前体检 → 编排采集 → 导出 → 采集量监控。
        供调度守护线程与手动「立即运行」共用，确保两条路径防护一致、不被绕过。"""
        tid = task["id"]
        out: Dict = {"status": task_store.STATUS_DONE, "export_path": "",
                     "stopped": False, "blocked": False, "result": None}
        orchestrator.clear_stop(tid)

        # 任务前登录体检：失效平台跳过并继续其余平台，只有全部失效才整体失败
        login_ok = self._preflight_login(task)
        invalid = [p for p, ok in login_ok.items() if not ok]
        run_platforms = [p for p in task["platforms"] if p not in invalid]
        out["skipped"] = invalid
        if invalid:
            from tools import login_state
            names = [login_state.PLATFORM_LOGIN_INFO[p]["name"] for p in invalid]
            for p in invalid:
                task_store.update_platform_status(tid, p, {
                    "status": "failed",
                    "contents": 0,
                    "comments": 0,
                    "message": "登录态失效，本轮跳过，请先扫码",
                })
            if not run_platforms:
                out["status"] = task_store.STATUS_FAILED
                out["blocked"] = True
                task_store.update_task(tid, {
                    "message": f"登录态失效（{'、'.join(names)}），请先扫码后再运行，本次未采集"
                })
                return out
            task_store.update_task(tid, {
                "message": f"{'、'.join(names)}登录态失效已跳过，继续采集其余平台"
            })

        result = orchestrator.run_once(task, win_start, win_end, deadline_ts,
                                       platforms=run_platforms)
        out["result"] = result
        if result["stopped"]:
            out["status"] = task_store.STATUS_STOPPED
            out["stopped"] = True
        else:
            from tools import task_exporter
            out["export_path"] = task_exporter.export_task(tid, win_start, win_end)

        # 采集量异常监控：先评估（历史不含本次）再记录
        from tools import collect_metrics
        warnings = []
        for plat, res in result["results"].items():
            c = int(res.get("contents", 0))
            m = int(res.get("comments", 0))
            ev = collect_metrics.evaluate(plat, c)
            if ev["level"] == "warning":
                warnings.append(f"{plat}:{ev['message']}")
            collect_metrics.record_run(tid, plat, c, m)
        if warnings:
            task_store.update_task(tid, {
                "message": "采集量告警：" + "；".join(warnings)
            })
        return out

    def _worker(self, task_id: str, due: Dict) -> None:
        export_path = ""
        overall_status = task_store.STATUS_DONE
        history_entry = {
            "run_date": due["run_date"],
            "started_at": time_util.get_current_time(),
            "ended_at": "",
            "status": "",
            "export_file": "",
        }
        try:
            task = task_store.get_task(task_id)
            if task is None:
                return
            task_store.update_task(task_id, {"status": task_store.STATUS_RUNNING,
                                             "current_platform": "", "message": "开始执行"})
            exec_out = self.execute_once(
                task, due["win_start"], due["win_end"], due["deadline_ts"]
            )
            overall_status = exec_out["status"]
            export_path = exec_out["export_path"]

            history_entry["status"] = overall_status
            history_entry["export_file"] = export_path
            history_entry["ended_at"] = time_util.get_current_time()

            task = task_store.get_task(task_id)
            history = task.get("run_history", [])
            history.append(history_entry)
            export_files = task.get("export_files", [])
            if export_path:
                export_files.append(export_path)

            if task["schedule_type"] == task_store.SCHED_DAILY and overall_status != task_store.STATUS_STOPPED:
                # 每日任务：回到待运行，等待下一天
                final_status = task_store.STATUS_PENDING
            else:
                final_status = overall_status
            base_msg = "已停止" if overall_status == task_store.STATUS_STOPPED else "本轮完成"
            skipped = exec_out.get("skipped") or []
            if skipped:
                from tools import login_state
                snames = [login_state.PLATFORM_LOGIN_INFO[p]["name"] for p in skipped]
                base_msg += f"（{'、'.join(snames)}登录态失效已跳过）"
            task_store.update_task(task_id, {
                "status": final_status,
                "run_history": history,
                "export_files": export_files,
                "current_platform": "",
                # 写回本轮真实窗口：卡片显示与「导出」按钮跟上最新一轮（每日任务随日期滚动）
                "time_start": due["win_start"],
                "time_end": due["win_end"],
                "message": base_msg,
            })
        except Exception as e:
            print(f"[Scheduler] worker error: {e}")
            task_store.update_task(task_id, {"status": task_store.STATUS_FAILED,
                                             "message": f"执行失败：{e}"})
        finally:
            self._active_task_id = None


# 全局单例
scheduler = Scheduler()
