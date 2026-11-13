# -*- coding: utf-8 -*-
"""批 10 单测：
1. 「最新」排序悬停面板点击（面板缺失/坐标点击生效/JS 兜底）；
2. /api/status 进度计数与 jsonl 落盘计数同步（问题 1）；
3. 编排器运行中把落盘计数写回任务卡、停止时返回真实已采数（问题 1）。
"""
import asyncio

import pytest


# ---------- 「最新」排序 ----------

class FakeMouse:
    def __init__(self):
        self.moves = []
        self.clicks = []

    async def move(self, x, y):
        self.moves.append((x, y))

    async def click(self, x, y):
        self.clicks.append((x, y))


class FakeFilterLocator:
    def __init__(self):
        self.first = self

    async def hover(self, timeout=None):
        pass

    async def bounding_box(self):
        return {"x": 1700, "y": 90, "width": 60, "height": 30}


class FakeSortPage:
    def __init__(self, boxes, active_labels):
        self.boxes = boxes            # label -> 包围盒（None/缺省=面板无此选项）
        self.active_labels = set(active_labels)
        self.js_clicked_labels = []
        self.mouse = FakeMouse()

    def locator(self, sel):
        assert sel == ".filter"
        return FakeFilterLocator()

    async def evaluate(self, js, arg=None, isolated_context=None):
        if "getBoundingClientRect" in js:
            return self.boxes.get(arg)
        if "includes('active')" in js:
            return arg in self.active_labels
        if ".click()" in js:
            self.js_clicked_labels.append(arg)
            return True
        return None


@pytest.fixture
def sort_env(monkeypatch):
    from media_platform.xhs import ui_bridge
    from media_platform.xhs.ui_bridge import XhsUIBridge

    async def fast_sleep(_):
        pass
    monkeypatch.setattr(ui_bridge.asyncio, "sleep", fast_sleep)

    class P:
        def on(self, *a, **k):
            pass
    bridge = XhsUIBridge(context=None, main_page=P(),
                         index_url="https://www.xiaohongshu.com")
    return bridge


BOX = {"x": 1472, "y": 187, "w": 96, "h": 40}
BOX_TIME = {"x": 1472, "y": 411, "w": 96, "h": 40}


def _center(box):
    return box["x"] + box["w"] / 2, box["y"] + box["h"] / 2


class TestLatestSort:
    def test_panel_missing_returns_false(self, sort_env):
        sp = FakeSortPage(boxes={}, active_labels=[])
        ok = asyncio.run(sort_env._apply_search_filters(sp, ""))
        assert ok is False
        assert sp.mouse.clicks == []

    def test_coord_click_active_no_fallback(self, sort_env):
        sp = FakeSortPage(boxes={"最新": BOX}, active_labels=["最新"])
        ok = asyncio.run(sort_env._apply_search_filters(sp, ""))
        assert ok is True
        assert len(sp.mouse.clicks) == 1
        cx, cy = sp.mouse.clicks[0]
        ex, ey = _center(BOX)
        assert abs(cx - ex) < 1 and abs(cy - ey) < 1
        assert sp.js_clicked_labels == []
        # 收尾移开鼠标收面板
        last_x, last_y = sp.mouse.moves[-1]
        assert 400 <= last_x <= 800 and 500 <= last_y <= 700

    def test_js_fallback_when_not_active(self, sort_env):
        sp = FakeSortPage(boxes={"最新": BOX}, active_labels=[])
        ok = asyncio.run(sort_env._apply_search_filters(sp, ""))
        assert ok is True
        assert sp.js_clicked_labels == ["最新"]

    def test_dual_filters_latest_and_time(self, sort_env):
        sp = FakeSortPage(boxes={"最新": BOX, "一天内": BOX_TIME},
                          active_labels=["最新", "一天内"])
        ok = asyncio.run(sort_env._apply_search_filters(sp, "一天内"))
        assert ok is True
        assert len(sp.mouse.clicks) == 2
        for click, box in zip(sp.mouse.clicks, (BOX, BOX_TIME)):
            ex, ey = _center(box)
            assert abs(click[0] - ex) < 1 and abs(click[1] - ey) < 1
        assert sp.js_clicked_labels == []

    def test_time_tag_missing_keeps_sort(self, sort_env):
        sp = FakeSortPage(boxes={"最新": BOX}, active_labels=["最新"])
        ok = asyncio.run(sort_env._apply_search_filters(sp, "一周内"))
        assert ok is True           # 排序仍生效
        assert len(sp.mouse.clicks) == 1  # 发布时间选项缺失只点了一次


class TestTimeFilterMapping:
    def test_today_window(self):
        from media_platform.xhs.ui_bridge import time_filter_for_window
        assert time_filter_for_window("2026-11-13 00:00:00",
                                      "2026-11-13 23:59:59") == "一天内"

    def test_seven_day_window(self):
        from media_platform.xhs.ui_bridge import time_filter_for_window
        assert time_filter_for_window("2026-11-07 00:00:00",
                                      "2026-11-13 23:59:59") == "一周内"

    def test_month_window(self):
        from media_platform.xhs.ui_bridge import time_filter_for_window
        assert time_filter_for_window("2026-10-14 00:00:00",
                                      "2026-11-13 23:59:59") == "半年内"

    def test_long_window_no_filter(self):
        from media_platform.xhs.ui_bridge import time_filter_for_window
        assert time_filter_for_window("2025-01-01 00:00:00",
                                      "2026-11-13 23:59:59") == ""

    def test_incomplete_window_no_filter(self):
        from media_platform.xhs.ui_bridge import time_filter_for_window
        assert time_filter_for_window("", "2026-11-13 23:59:59") == ""
        assert time_filter_for_window("2026-11-13 00:00:00", "") == ""
        assert time_filter_for_window("2026-11-13 23:59:59",
                                      "2026-11-13 00:00:00") == ""


# ---------- /api/status 计数同步 ----------

class TestStatusCountSync:
    def test_progress_counts_synced_with_live(self, monkeypatch, tmp_path):
        import server
        import tools.live_count as lc

        task = {"id": "T1", "name": "t", "platforms": ["xhs"],
                "current_platform": "xhs", "data_mode": "content",
                "crawl_mode": "login",
                "platform_status": {"xhs": {"started_ts": 1000.0}}}
        monkeypatch.setattr(server.scheduler, "_active_task_id", "T1")
        monkeypatch.setattr(server.task_store, "get_task", lambda tid: task)
        (tmp_path / "orchestrator_T1_xhs.log").write_text(
            "Launching browser\nBegin pong\n"
            "Current search keyword: 测试\nBegin get note detail\n",
            encoding="utf-8")
        monkeypatch.setattr(server, "DATA", tmp_path)
        monkeypatch.setattr(server, "state", {"proc": None, "meta": {}})
        monkeypatch.setattr(lc, "live_counts",
                            lambda p, ts, mode="content": {"saved": 5, "comment": 2})

        r = server.status()
        assert r["live_counts"]["saved"] == 5
        # 日志里没有任何落盘行，旧口径会是 0；同步后必须等于落盘计数
        assert r["progress"]["counts"]["saved"] == 5
        collect = [m for m in r["progress"]["timeline"] if m["key"] == "collect"]
        assert collect and collect[0]["counts"]["saved"] == 5


# ---------- 编排器运行中计数回写 ----------

class TestOrchestratorLiveCounts:
    def _setup(self, monkeypatch, tmp_path, polls, stopped=False):
        from tools import orchestrator as orch

        (tmp_path / "data").mkdir(exist_ok=True)
        monkeypatch.setattr(orch, "ROOT", str(tmp_path))
        monkeypatch.setattr(orch, "build_cmd", lambda *a, **k: ["fake"])

        it = iter(polls)

        class FakeProc:
            pid = 4321  # 真实 Popen 必有 pid（PID 落盘兜底停止用）

            def poll(self):
                return next(it)
        monkeypatch.setattr(orch.subprocess, "Popen", lambda *a, **k: FakeProc())
        monkeypatch.setattr(orch, "_is_stop", lambda tid: stopped)
        monkeypatch.setattr(orch, "_kill_tree", lambda p: None)
        monkeypatch.setattr(orch.time, "sleep", lambda s: None)
        updates = []
        monkeypatch.setattr(orch.task_store, "update_platform_status",
                            lambda tid, p, info: updates.append(dict(info)))
        monkeypatch.setattr(orch, "live_counts",
                            lambda p, ts, mode="content": {"saved": 3, "comment": 1})
        monkeypatch.setattr(orch.task_exporter, "count_task_rows",
                            lambda *a, **k: (3, 1))
        monkeypatch.setattr(orch.risk_signals, "count_high_risk", lambda *a: 0)
        task = {"id": "T1", "data_mode": "content"}
        return orch, task, updates

    def test_running_writes_live_counts(self, monkeypatch, tmp_path):
        orch, task, updates = self._setup(monkeypatch, tmp_path, [None, None, 0])
        res = orch.run_platform(task, "xhs", "2026-11-13 00:00:00",
                                "2026-11-13 23:59:59")
        assert res["status"] == "done"
        assert res["contents"] == 3
        # 运行中至少一次把落盘计数写回任务卡
        assert any(u.get("contents") == 3 and u.get("comments") == 1
                   for u in updates)

    def test_stop_returns_real_counts(self, monkeypatch, tmp_path):
        orch, task, updates = self._setup(monkeypatch, tmp_path, [None],
                                          stopped=True)
        res = orch.run_platform(task, "xhs", "2026-11-13 00:00:00",
                                "2026-11-13 23:59:59")
        assert res["status"] == "stopped"
        # 停止不再返回 0，而是本轮真实已采数
        assert res["contents"] == 3
        assert res["comments"] == 1