# -*- coding: utf-8 -*-
# 批7单元测试：关键词全角逗号归一化 / xhs 窗口搜索（最新排序+窗口内配额+末页处理）/ 体检异常沿用缓存。
import asyncio
from datetime import datetime

import pytest

import config
from tools import task_store, login_state


@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
    return tmp_path


class TestKeywordNormalization:
    def test_fullwidth_commas_normalized(self, store_tmp):
        t = task_store.create_task({"platforms": ["xhs"], "keywords": "遴选，公选王，北辰",
                                    "crawl_mode": "login", "data_mode": "content",
                                    "input_type": "keyword", "schedule_type": "immediate"})
        assert t["keywords"] == "遴选,公选王,北辰"
        # 归一化后逐词与导出过滤口径一致
        from tools.task_exporter import _task_keyword_set
        assert _task_keyword_set(t) == {"遴选", "公选王", "北辰"}

    def test_mixed_separators_and_spaces(self, store_tmp):
        t = task_store.create_task({"platforms": ["xhs"], "keywords": "遴选；公选王 北辰,公选",
                                    "crawl_mode": "login", "data_mode": "content",
                                    "input_type": "keyword", "schedule_type": "immediate"})
        assert t["keywords"] == "遴选,公选王,北辰,公选"

    def test_empty_keywords_still_rejected(self, store_tmp):
        with pytest.raises(ValueError):
            task_store.create_task({"platforms": ["xhs"], "keywords": "，，",
                                    "crawl_mode": "login", "data_mode": "content",
                                    "input_type": "keyword", "schedule_type": "immediate"})


def _ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()) * 1000


class TestXhsWindowSearch:
    @pytest.fixture
    def crawler(self, monkeypatch):
        from media_platform.xhs.core import XiaoHongShuCrawler
        from store import xhs as xhs_store

        c = XiaoHongShuCrawler()
        calls = {"sorts": [], "pages": [], "saved": []}

        async def fake_keyword(keyword, search_id, page, sort, time_filter=""):
            calls["sorts"].append(sort.value)
            calls["pages"].append(page)
            if page == 1:
                return {"has_more": True,
                        "items": [{"id": "old1", "model_type": "note", "xsec_token": "t"}]}
            return {"has_more": False,
                    "items": [{"id": "new1", "model_type": "note", "xsec_token": "t"}]}

        async def fake_detail(self, note_id, xsec_source, xsec_token, semaphore):
            t = _ts("2026-11-01 10:00:00") if note_id == "old1" else _ts("2026-11-12 10:00:00")
            return {"note_id": note_id, "time": t, "xsec_token": "t"}

        async def fake_save(note):
            calls["saved"].append(note["note_id"])

        async def noop(*a, **k):
            pass

        class FakeClient:
            get_note_by_keyword = staticmethod(fake_keyword)
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
        monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 2)
        return c, calls

    def test_window_mode_latest_sort_and_paging(self, crawler, monkeypatch):
        c, calls = crawler
        monkeypatch.setattr(config, "TIME_START", "2026-11-12 00:00:00")
        monkeypatch.setattr(config, "TIME_END", "2026-11-12 23:59:59")
        asyncio.run(c.search())
        assert calls["sorts"][0] == "time_descending"  # 窗口模式按最新排序
        assert calls["pages"] == [1, 2]  # 首页全旧帖不再直接结束，继续翻找
        assert calls["saved"] == ["new1"]  # 只入库窗口内帖子

    def test_no_window_general_sort_single_page(self, crawler, monkeypatch):
        c, calls = crawler
        monkeypatch.setattr(config, "TIME_START", "")
        monkeypatch.setattr(config, "TIME_END", "")
        asyncio.run(c.search())
        assert calls["sorts"][0] == "general"
        assert calls["pages"] == [1]  # 无窗口保持原单页行为
        assert calls["saved"] == ["old1"]  # 无窗口不过滤

    def test_last_page_items_processed(self, crawler, monkeypatch):
        c, calls = crawler
        monkeypatch.setattr(config, "TIME_START", "")
        monkeypatch.setattr(config, "TIME_END", "")

        async def only_last_page(keyword, search_id, page, sort, time_filter=""):
            return {"has_more": False,
                    "items": [{"id": "new1", "model_type": "note", "xsec_token": "t"}]}

        c.xhs_client.get_note_by_keyword = only_last_page
        asyncio.run(c.search())
        assert calls["saved"] == ["new1"]  # 末页（has_more=False）不再整页丢弃


class TestHealthCheckFallback:
    def test_check_exception_keeps_cached_conclusion(self, tmp_path, monkeypatch):
        monkeypatch.setattr(login_state, "STATE_FILE", str(tmp_path / "login_state.json"))
        # 预置缓存：bili 已登录
        login_state._merge("bili", {"logged_in": True, "profile_exists": True,
                                    "last_checked": "2026-11-12 15:06:27", "detail": "登录有效"})
        # profile 目录需存在才会走浏览器体检
        monkeypatch.setattr(login_state, "_profile_dir", lambda plat: str(tmp_path))

        class Boom:
            async def __aenter__(self):
                raise RuntimeError("net::ERR_ABORTED")

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(login_state, "async_playwright", lambda: Boom())
        r = asyncio.run(login_state.check_platform("bili"))
        assert r["logged_in"] is True  # 体检异常不推翻缓存结论
        assert "沿用上次结论" in r["detail"]