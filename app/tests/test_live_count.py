# -*- coding: utf-8 -*-
# 实时采集计数单测：本轮过滤 / 帖子vs评论模式 / 缺字段保守计入 / 旧文件跳过。
import json
import os
import time

import pytest

from tools import live_count


@pytest.fixture
def data_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    jdir = tmp_path / "data" / "xhs" / "jsonl"
    jdir.mkdir(parents=True)
    return jdir


def _write(jdir, name, rows, mtime=None):
    p = jdir / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_counts_only_this_run(data_tmp):
    now = time.time()
    since = now - 100
    old_ms = int((now - 200) * 1000)
    new_ms = int((now - 50) * 1000)
    _write(data_tmp, "search_contents_2026-11-13.jsonl", [
        {"note_id": "old", "last_modify_ts": old_ms},
        {"note_id": "new1", "last_modify_ts": new_ms},
        {"note_id": "new2", "last_modify_ts": new_ms},
    ], mtime=now)
    c = live_count.live_counts("xhs", since, "content")
    assert c["saved"] == 2  # 仅本轮 new1/new2
    assert c["comment"] == 0


def test_comment_mode(data_tmp):
    now = time.time()
    since = now - 100
    new_ms = int((now - 50) * 1000)
    _write(data_tmp, "detail_comments_2026-11-13.jsonl",
           [{"comment_id": "c1", "last_modify_ts": new_ms}], mtime=now)
    _write(data_tmp, "search_contents_2026-11-13.jsonl",
           [{"note_id": "n1", "last_modify_ts": new_ms}], mtime=now)
    c = live_count.live_counts("xhs", since, "comment")
    assert c["comment"] == 1
    assert c["saved"] == 0  # 评论模式不统计帖子


def test_missing_last_modify_ts_counted(data_tmp):
    now = time.time()
    _write(data_tmp, "search_contents_x.jsonl", [{"note_id": "n1"}], mtime=now)
    c = live_count.live_counts("xhs", now - 100, "content")
    assert c["saved"] == 1  # 缺字段保守计入


def test_old_file_skipped(data_tmp):
    now = time.time()
    _write(data_tmp, "search_contents_old.jsonl",
           [{"note_id": "n", "last_modify_ts": int(now * 1000)}], mtime=now - 500)
    c = live_count.live_counts("xhs", now - 100, "content")
    assert c["saved"] == 0  # 文件修改时间早于本轮起点，跳过


def test_no_since_returns_zero(data_tmp):
    assert live_count.live_counts("xhs", 0, "content") == {"saved": 0, "comment": 0}


def test_corrupt_line_tolerated(data_tmp):
    now = time.time()
    p = data_tmp / "search_contents_bad.jsonl"
    p.write_text('{"note_id":"ok","last_modify_ts":%d}\nnot-json\n' % int(now * 1000), encoding="utf-8")
    os.utime(p, (now, now))
    c = live_count.live_counts("xhs", now - 100, "content")
    assert c["saved"] == 1  # 坏行跳过，好行计入