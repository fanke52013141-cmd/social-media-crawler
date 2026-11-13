# -*- coding: utf-8 -*-
# 进度详情查看计数单测：抓详情阶段累计 viewed，落盘前即可见进展。
from tools import progress


def test_viewed_counts_detail_fetches():
    lines = [
        "2026-11-13 10:00:00 MediaCrawler INFO - [XiaoHongShuCrawler.search] Begin search",
        "2026-11-13 10:00:01 MediaCrawler INFO - [get_note_detail_async_task] Begin get note detail, note_id: a",
        "2026-11-13 10:00:02 MediaCrawler INFO - [get_note_detail_async_task] Begin get note detail, note_id: b",
        "2026-11-13 10:00:03 MediaCrawler INFO - [get_note_detail_async_task] Begin get note detail, note_id: c",
    ]
    pg = progress.interpret(lines, "小红书", data_mode="content")
    assert pg["counts"]["viewed"] == 3


def test_viewed_zero_when_no_detail():
    lines = ["2026-11-13 10:00:00 MediaCrawler INFO - Launching browser"]
    pg = progress.interpret(lines, "小红书", data_mode="content")
    assert pg["counts"]["viewed"] == 0