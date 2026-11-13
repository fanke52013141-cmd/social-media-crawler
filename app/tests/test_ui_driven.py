# -*- coding: utf-8 -*-
# UI 驱动改造配套测试：时间窗早停 / 定时任务抖动 / UI 桥接层风控语义。
import asyncio
from datetime import datetime, timedelta

import pytest

import config
from tools import task_store


def _ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()) * 1000


class TestWindowEarlyStop:
    @pytest.fixture
    def crawler(self, monkeypatch):
        from media_platform.xhs.core import XiaoHongShuCrawler
        from store import xhs as xhs_store

        c = XiaoHongShuCrawler()
        calls = {"pages": [], "saved": []}

        async def fake_detail(self, note_id, xsec_source, xsec_token, semaphore):
            # old* 为窗口外（11-01），new* 为窗口内（11-12）
            day = "2026-11-01" if note_id.startswith("old") else "2026-11-12"
            return {"note_id": note_id, "time": _ts(f"{day} 10:00:00"),
                    "xsec_token": "t"}

        async def fake_save(note):
            calls["saved"].append(note["note_id"])

        async def noop(*a, **k):
            pass

        class FakeClient:
            _random_sleep = staticmethod(noop)

        c.xhs_client = FakeClient()
        monkeypatch.setattr(XiaoHongShuCrawler, "get_note_detail_async_task", fake_detail)
        monkeypatch.setattr(XiaoHongShuCrawler, "download_media", noop)
        monkeypatch.setattr(XiaoHongShuCrawler, "batch_get_note_comments", noop)
        monkeypatch.setattr(xhs_store, "update_xhs_note", fake_save)
        monkeypatch.setattr(config, "KEYWORDS", "遴选")
        monkeypatch.setattr(config, "START_PAGE", 1)
        monkeypatch.setattr(config, "SORT_TYPE", "")
        monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
        monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
        monkeypatch.setattr(config, "WINDOW_EARLY_STOP_STREAK", 5)
        monkeypatch.setattr(config, "TIME_START", "2026-11-12 00:00:00")
        monkeypatch.setattr(config, "TIME_END", "2026-11-12 23:59:59")
        return c, calls

    def _set_pages(self, c, calls, pages):
        async def fake_keyword(keyword, search_id, page, sort, time_filter=""):
            calls["pages"].append(page)
            return pages[page - 1]
        c.xhs_client.get_note_by_keyword = fake_keyword

    @staticmethod
    def _items(ids, has_more=True):
        return {"has_more": has_more,
                "items": [{"id": i, "model_type": "note", "xsec_token": "t"}
                          for i in ids]}

    def test_streak_stops_paging(self, crawler):
        c, calls = crawler
        # 首页连续 5 条窗口外 → 早停，不再翻第 2 页
        self._set_pages(c, calls, [
            self._items(["old1", "old2", "old3", "old4", "old5"]),
            self._items(["new1"]),
        ])
        asyncio.run(c.search())
        assert calls["pages"] == [1]
        assert calls["saved"] == []

    def test_streak_resets_on_in_window(self, crawler):
        c, calls = crawler
        # 4 旧 + 1 新（计数清零）→ 第 2 页 5 旧才早停；窗口内数据一条不少
        self._set_pages(c, calls, [
            self._items(["old1", "old2", "old3", "old4", "new1"]),
            self._items(["old5", "old6", "old7", "old8", "old9"]),
            self._items(["new2"]),
        ])
        asyncio.run(c.search())
        assert calls["pages"] == [1, 2]
        assert calls["saved"] == ["new1"]

    def test_window_overrides_popularity_sort(self, crawler, monkeypatch):
        c, calls = crawler
        # 即使 SORT_TYPE 配置为热度排序，窗口模式仍强制最新（早停依赖时序）
        monkeypatch.setattr(config, "SORT_TYPE", "popularity_descending")
        sorts = []
        time_filters = []

        async def fake_keyword(keyword, search_id, page, sort, time_filter=""):
            sorts.append(sort.value)
            time_filters.append(time_filter)
            return self._items(["new1"], has_more=False)

        c.xhs_client.get_note_by_keyword = fake_keyword
        asyncio.run(c.search())
        assert sorts == ["time_descending"]
        # 一天窗口 → 平台侧「发布时间」预筛选项为一天内
        assert time_filters == ["一天内"]
        assert calls["saved"] == ["new1"]

    def test_below_streak_keeps_paging(self, crawler):
        c, calls = crawler
        # 每页 4 旧（<5）+ 末页新帖：不早停，正常翻到末页
        self._set_pages(c, calls, [
            self._items(["old1", "old2", "old3", "old4"]),
            self._items(["new1"], has_more=False),
        ])
        asyncio.run(c.search())
        assert calls["pages"] == [1, 2]
        assert calls["saved"] == ["new1"]


class TestSchedulerJitter:
    @pytest.fixture
    def daily_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
        monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
        t = task_store.create_task({
            "platforms": ["xhs"], "keywords": "遴选", "crawl_mode": "login",
            "data_mode": "content", "input_type": "keyword",
            "schedule_type": "daily", "run_time": "08:00",
        })
        # 固定生效日期，避免"创建时当天时刻已过→明天生效"影响断言
        t["start_date"] = "2026-11-01"
        return t

    def test_never_fires_before_run_time(self, daily_task):
        from tools.scheduler import Scheduler
        s = Scheduler()
        now = datetime(2026, 11, 12, 7, 59)
        assert s._check_due(daily_task, now) is None

    def test_jitter_deterministic_same_day(self, daily_task):
        from tools.scheduler import Scheduler
        s = Scheduler()
        now = datetime(2026, 11, 12, 8, 30)
        r1 = s._check_due(daily_task, now)
        r2 = s._check_due(daily_task, now)
        assert (r1 is None) == (r2 is None)
        if r1 is not None:
            assert r1["win_start"] == r2["win_start"]

    def test_fires_after_max_jitter(self, daily_task, monkeypatch):
        from tools.scheduler import Scheduler
        monkeypatch.setattr(config, "SCHED_JITTER_MAX_MIN", 90)
        s = Scheduler()
        now = datetime(2026, 11, 12, 8, 0) + timedelta(minutes=91)
        due = s._check_due(daily_task, now)
        assert due is not None
        # 窗口仍按原执行时刻回推（当天），不受抖动影响
        assert due["win_start"].startswith("2026-11-12")

    def test_jitter_disabled(self, daily_task, monkeypatch):
        from tools.scheduler import Scheduler
        monkeypatch.setattr(config, "SCHED_JITTER_MAX_MIN", 0)
        s = Scheduler()
        due = s._check_due(daily_task, datetime(2026, 11, 12, 8, 0))
        assert due is not None


class TestUIBridgeRiskSemantics:
    @pytest.fixture
    def bridge(self, monkeypatch):
        from media_platform.xhs import ui_bridge
        from media_platform.xhs.ui_bridge import XhsUIBridge

        monkeypatch.setattr(ui_bridge.risk_signals, "record", lambda *a, **k: None)

        class FakePage:
            def on(self, *a, **k):
                pass

        return XhsUIBridge(context=None, main_page=FakePage(),
                           index_url="https://www.xiaohongshu.com")

    def test_success_returns_data(self, bridge):
        r = asyncio.run(bridge._check(200, {}, {"success": True, "data": {"x": 1}}))
        assert r == {"x": 1}

    def test_security_limit_circuit_breaker(self, bridge):
        from media_platform.xhs.exception import RiskControlError
        with pytest.raises(RiskControlError):
            asyncio.run(bridge._check(200, {}, {"success": False, "code": 300011}))

    def test_rate_limit_circuit_breaker(self, bridge):
        from media_platform.xhs.exception import RiskControlError
        with pytest.raises(RiskControlError):
            asyncio.run(bridge._check(429, {}, None))

    def test_note_not_found(self, bridge):
        from media_platform.xhs.exception import NoteNotFoundError
        with pytest.raises(NoteNotFoundError):
            asyncio.run(bridge._check(200, {}, {"success": False, "code": -510001}))

    def test_generic_failure(self, bridge):
        from media_platform.xhs.exception import DataFetchError
        with pytest.raises(DataFetchError):
            asyncio.run(bridge._check(200, {}, {"success": False, "code": -1, "msg": "x"}))

    def test_captcha_without_handler_raises(self, bridge):
        from media_platform.xhs.exception import CaptchaError
        with pytest.raises(CaptchaError):
            asyncio.run(bridge._check(461, {"verifytype": "1"}, None))