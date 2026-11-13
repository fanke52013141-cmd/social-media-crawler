# -*- coding: utf-8 -*-
# 进度漏斗计数单测：已查看按笔记去重、失败独立计数、
# 满足「已查看 = 已采集 + 已过滤 + 未入库」口径（未入库由前端做差）。
from tools import progress


def _line(ts, msg):
    return f"2026-11-13 {ts} MediaCrawler INFO - {msg}"


def test_viewed_dedup_across_keywords():
    # 同一笔记被两个关键词各查看一次，只算一篇
    lines = [
        _line("10:00:01", "[get_note_detail_async_task] Begin get note detail, note_id: aaa"),
        _line("10:05:01", "[get_note_detail_async_task] Begin get note detail, note_id: aaa"),
        _line("10:06:01", "[get_note_detail_async_task] Begin get note detail, note_id: bbb"),
    ]
    pg = progress.interpret(lines, "小红书", data_mode="content")
    assert pg["counts"]["viewed"] == 2


def test_failed_counted_and_dedup():
    lines = [
        _line("10:00:01", "[get_note_detail_async_task] Begin get note detail, note_id: aaa"),
        _line("10:00:02", "[skip] Failed to get note detail, Id: aaa, 跳过继续"),
        _line("10:00:03", "[get_note_detail_async_task] Begin get note detail, note_id: bbb"),
        _line("10:00:04", "[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: bbb, x"),
        _line("10:00:05", "[get_note_detail_async_task] Begin get note detail, note_id: ccc"),
        _line("10:00:06", "[XiaoHongShuCrawler.get_note_detail_async_task] Access restricted while getting note ccc: x"),
    ]
    pg = progress.interpret(lines, "小红书", data_mode="content")
    assert pg["counts"]["viewed"] == 3
    assert pg["counts"]["failed"] == 3
    # 失败有友好文案，不静默
    assert any("获取失败" in t["text"] for t in pg["timeline"])


def test_funnel_identity_with_skip():
    # viewed=3（aaa/bbb/ccc），skip=1（bbb 窗口外），saved 由 live_counts 提供=1（aaa）
    # 未入库 = 3-1-1 = 1（ccc 处理中）
    lines = [
        _line("10:00:01", "[get_note_detail_async_task] Begin get note detail, note_id: aaa"),
        _line("10:00:02", "[store.xhs.update_xhs_note] xhs note: {'note_id': 'aaa'}"),
        _line("10:00:03", "[get_note_detail_async_task] Begin get note detail, note_id: bbb"),
        _line("10:00:04", "[XiaoHongShuCrawler.search] skip out-of-window note bbb, time=1"),
        _line("10:00:05", "[get_note_detail_async_task] Begin get note detail, note_id: ccc"),
    ]
    pg = progress.interpret(lines, "小红书", data_mode="content")
    c = pg["counts"]
    assert c["viewed"] == 3 and c["skip"] == 1 and c["failed"] == 0
    pending = c["viewed"] - 1 - c["skip"]  # 1 = live saved
    assert pending == 1


def test_no_failure_no_regression():
    lines = [_line("10:00:00", "Launching browser")]
    pg = progress.interpret(lines, "小红书", data_mode="content")
    assert pg["counts"]["failed"] == 0
    assert pg["counts"]["viewed"] == 0