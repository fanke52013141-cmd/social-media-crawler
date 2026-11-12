# -*- coding: utf-8 -*-
# 批2单元测试：每日窗口当天自动滚动 / 近N天可选 / 计数-导出同口径 / 「全部」语义 / 定时触发截止修正。
import json
from datetime import datetime, timedelta

import pytest

from tools import task_store, task_exporter, time_util
from tools.scheduler import Scheduler


@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
    return tmp_path


def _payload(**kw):
    base = {"platforms": ["wb"], "keywords": "测试", "crawl_mode": "login",
            "data_mode": "content", "input_type": "keyword"}
    base.update(kw)
    return base


class TestComputeWindow:
    def test_daily_today_rolls_with_date(self):
        task = {"schedule_type": "daily", "window_mode": "today", "window_hours": 168}
        w1 = task_store.compute_window(task, datetime(2026, 11, 12, 9, 0, 0))
        w2 = task_store.compute_window(task, datetime(2026, 11, 13, 9, 0, 0))
        assert w1["win_start"] == "2026-11-12T00:00:00"
        assert w1["win_end"] == "2026-11-12T23:59:59"
        assert w2["win_start"] == "2026-11-13T00:00:00"
        assert w2["win_end"] == "2026-11-13T23:59:59"
        assert w1 != w2  # 跨天自动滚动
        # 收尾截止是当天终点而非执行时刻（修复「一触发就收尾、一个平台都不采」）
        assert w1["deadline_ts"] == time_util.iso_to_ts("2026-11-12T23:59:59")
        assert w1["deadline_ts"] > int(datetime(2026, 11, 12, 9, 0, 0).timestamp())

    def test_daily_today_default_without_window_mode(self):
        # 旧任务无 window_mode 字段：默认当天
        task = {"schedule_type": "daily", "window_hours": 168}
        w = task_store.compute_window(task, datetime(2026, 11, 12, 9, 0))
        assert w["win_start"] == "2026-11-12T00:00:00"
        assert w["win_end"] == "2026-11-12T23:59:59"

    def test_daily_rolling(self):
        task = {"schedule_type": "daily", "window_mode": "rolling", "window_hours": 72}
        run = datetime(2026, 11, 12, 9, 0, 0)
        w = task_store.compute_window(task, run)
        assert w["win_start"] == "2026-11-09T09:00:00"
        assert w["win_end"] == "2026-11-12T09:00:00"
        assert w["deadline_ts"] is None  # 滚动模式窗口终点即执行时刻，不设收尾截止

    def test_once_uses_fixed_absolute_window(self):
        task = {"schedule_type": "once", "time_start": "2026-11-01 00:00:00",
                "time_end": "2026-11-05 23:59:59", "window_hours": 168}
        w = task_store.compute_window(task, datetime(2026, 11, 12, 9, 0))
        assert w["win_start"] == "2026-11-01 00:00:00"
        assert w["win_end"] == "2026-11-05 23:59:59"
        assert w["deadline_ts"] == time_util.iso_to_ts("2026-11-05 23:59:59")

    def test_immediate_empty_window_means_no_filter(self):
        task = {"schedule_type": "immediate", "time_start": "", "time_end": "",
                "window_hours": 168}
        w = task_store.compute_window(task, datetime(2026, 11, 12, 9, 0))
        assert w["win_start"] == "" and w["win_end"] == ""
        assert w["deadline_ts"] is None


class TestCreateTask:
    def test_daily_default_today_initial_window(self, store_tmp):
        t = task_store.create_task(_payload(schedule_type="daily", run_time="09:00"))
        assert t["window_mode"] == "today"
        today = datetime.now().strftime("%Y-%m-%d")
        assert t["time_start"] == today + "T00:00:00"
        assert t["time_end"] == today + "T23:59:59"

    def test_daily_rolling_initial_window(self, store_tmp):
        t = task_store.create_task(_payload(schedule_type="daily", run_time="09:00",
                                            window_mode="rolling", window_hours=24))
        assert t["window_mode"] == "rolling"
        assert "T" in t["time_start"] and "T" in t["time_end"]

    def test_invalid_window_mode_rejected(self, store_tmp):
        with pytest.raises(ValueError):
            task_store.create_task(_payload(schedule_type="daily", run_time="09:00",
                                            window_mode="bogus"))

    def test_immediate_all_no_longer_falls_back_to_168h(self, store_tmp):
        t = task_store.create_task(_payload(schedule_type="immediate"))
        assert t["time_start"] == "" and t["time_end"] == ""

    def test_once_fixed_dates(self, store_tmp):
        t = task_store.create_task(_payload(schedule_type="once",
                                            run_time="2026-12-01T09:00:00",
                                            time_start="2026-11-20 00:00:00",
                                            time_end="2026-11-25 23:59:59"))
        assert t["time_start"] == "2026-11-20 00:00:00"
        assert t["time_end"] == "2026-11-25 23:59:59"


class TestSchedulerDue:
    def test_daily_due_returns_today_window(self, store_tmp):
        t = task_store.create_task(_payload(schedule_type="daily", run_time="09:00"))
        # 强制生效日期为今天，保证可触发
        task_store.update_task(t["id"], {"start_date": datetime.now().strftime("%Y-%m-%d")})
        t = task_store.get_task(t["id"])
        now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        due = Scheduler()._check_due(t, now)
        assert due is not None
        today = now.strftime("%Y-%m-%d")
        assert due["win_start"] == today + "T00:00:00"
        assert due["win_end"] == today + "T23:59:59"
        assert due["deadline_ts"] > int(now.timestamp())  # 触发时不会立即收尾

    def test_once_due_returns_stored_window(self, store_tmp):
        past = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        t = task_store.create_task(_payload(schedule_type="once", run_time=past,
                                            time_start="2026-11-01 00:00:00",
                                            time_end="2026-11-30 23:59:59"))
        due = Scheduler()._check_due(t, datetime.now())
        assert due is not None
        assert due["win_start"] == "2026-11-01 00:00:00"
        assert due["win_end"] == "2026-11-30 23:59:59"


class TestCountExportConsistency:
    @pytest.fixture
    def data_tmp(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        jdir = tmp_path / "data" / "weibo" / "jsonl"
        jdir.mkdir(parents=True)
        base = datetime(2026, 11, 12, 10, 0, 0)
        rows = []
        for i in range(5):
            ts = int((base - timedelta(days=i)).timestamp())
            rows.append({"note_id": f"n{i}", "content": f"帖子{i}", "create_time": ts,
                         "nickname": "用户", "note_url": f"https://weibo.com/n{i}",
                         "source_keyword": "测试"})
        (jdir / "search_contents_2026-11-12.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return tmp_path

    def test_explicit_window_overrides_stored(self, data_tmp):
        task = {"id": "T1", "platforms": ["wb"], "input_type": "keyword", "keywords": "测试",
                "data_mode": "content",
                "time_start": "2026-11-01 00:00:00", "time_end": "2026-11-02 23:59:59"}
        # 存储的旧窗口不含任何数据（数据发布于 11-08 至 11-12）
        assert task_exporter.count_task_rows(task, "wb") == (0, 0)
        # 显式传入本轮真实窗口（11-12 当天）→ 1 条
        c, m = task_exporter.count_task_rows(task, "wb",
                                             "2026-11-12 00:00:00", "2026-11-12 23:59:59")
        assert (c, m) == (1, 0)

    def test_count_matches_export_rows(self, data_tmp, store_tmp):
        task = task_store.create_task(_payload(schedule_type="immediate",
                                               time_start="2026-11-10 00:00:00",
                                               time_end="2026-11-12 23:59:59"))
        c, m = task_exporter.count_task_rows(task, "wb")
        assert c == 3  # 11-10 / 11-11 / 11-12 各一条
        xlsx = task_exporter.export_task(task["id"], task["time_start"], task["time_end"])
        from openpyxl import load_workbook
        wb = load_workbook(xlsx, read_only=True)
        ws = wb["微博"]
        n = max(0, len(list(ws.iter_rows(values_only=True))) - 1)
        wb.close()
        assert n == c  # 卡片总数 == 导出 Excel 行数