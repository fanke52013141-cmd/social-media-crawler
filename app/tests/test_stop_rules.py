# -*- coding: utf-8 -*-
# 停止规则单测：单平台时长上限、deadline 打断当前平台、跨关键词详情去重、
# 服务重启后孤儿进程兜底停止（PID 文件机制）。
import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import config
from tools import orchestrator


class FakeProc:
    def __init__(self):
        self.killed = False
        self.pid = 12345

    def poll(self):
        return -9 if self.killed else None


@pytest.fixture
def orch_env(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(orchestrator.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(orchestrator, "_kill_tree", lambda p: setattr(p, "killed", True))
    monkeypatch.setattr(orchestrator, "build_cmd", lambda *a, **k: ["python", "main.py"])
    monkeypatch.setattr(orchestrator, "live_counts", lambda *a, **k: {"saved": 3, "comment": 0})
    monkeypatch.setattr(orchestrator.task_exporter, "count_task_rows", lambda *a, **k: (3, 0))
    monkeypatch.setattr(orchestrator.risk_signals, "count_high_risk", lambda *a, **k: 0)
    monkeypatch.setattr(orchestrator.task_store, "update_platform_status", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.task_store, "update_task", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)
    monkeypatch.setattr(orchestrator, "_stop_flags", {})
    return proc


TASK = {"id": "T_stop", "data_mode": "content", "platforms": ["xhs"]}
WIN = ("2026-11-13 00:00:00", "2026-11-13 23:59:59")


def test_runtime_cap_graceful_stop(orch_env, monkeypatch):
    proc = orch_env
    monkeypatch.setattr(config, "PLATFORM_MAX_RUNTIME_MIN", 0.001)  # 0.06s 即触发
    res = orchestrator.run_platform(TASK, "xhs", *WIN)
    assert proc.killed is True
    assert res["status"] == "done"          # 主动停止按完成收尾，数据不丢
    assert "时长上限" in res["message"]
    assert res["contents"] == 3


def test_deadline_interrupts_current_platform(orch_env, monkeypatch):
    proc = orch_env
    monkeypatch.setattr(config, "PLATFORM_MAX_RUNTIME_MIN", 120)
    res = orchestrator.run_platform(TASK, "xhs", *WIN, deadline_ts=1.0)  # 早已过期
    assert proc.killed is True
    assert res["status"] == "done"
    assert "已到结束时间" in res["message"]


def test_run_once_passes_deadline(orch_env, monkeypatch):
    captured = {}

    def fake_run_platform(task, platform, ws, we, deadline_ts=None):
        captured["deadline"] = deadline_ts
        return {"status": "done", "contents": 0, "comments": 0, "message": ""}

    monkeypatch.setattr(orchestrator, "run_platform", fake_run_platform)
    monkeypatch.setattr(orchestrator.risk_signals, "in_cooldown", lambda p: False)
    future = orchestrator.time.time() + 3600
    orchestrator.run_once(TASK, *WIN, deadline_ts=future)
    assert captured["deadline"] == future


def _ts(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()) * 1000


@pytest.fixture
def crawler(monkeypatch):
    from media_platform.xhs.core import XiaoHongShuCrawler
    from store import xhs as xhs_store

    c = XiaoHongShuCrawler()
    calls = {"detail": [], "saved": []}

    async def fake_keyword(keyword, search_id, page, sort, time_filter=""):
        return {"has_more": False, "items": [
            {"id": "dup1", "model_type": "note", "xsec_token": "t"},
            {"id": "old1", "model_type": "note", "xsec_token": "t"},
        ]}

    async def fake_detail(self, note_id, xsec_source, xsec_token, semaphore):
        calls["detail"].append(note_id)
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
    monkeypatch.setattr(config, "KEYWORDS", "公选王,遴选")
    monkeypatch.setattr(config, "START_PAGE", 1)
    monkeypatch.setattr(config, "SORT_TYPE", "")
    monkeypatch.setattr(config, "CRAWLER_MAX_NOTES_COUNT", 20)
    monkeypatch.setattr(config, "MAX_CONCURRENCY_NUM", 1)
    monkeypatch.setattr(config, "TIME_START", "2026-11-12 00:00:00")
    monkeypatch.setattr(config, "TIME_END", "2026-11-12 23:59:59")
    return c, calls


def test_cross_keyword_dedup(crawler):
    c, calls = crawler
    asyncio.run(c.search())
    # 两个关键词返回同样的帖子：详情各只抓一次（含窗口外的 old1）
    assert sorted(calls["detail"]) == ["dup1", "old1"]
    assert calls["saved"] == ["dup1"]


def test_stop_orphan_kills_and_marks(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    pf = tmp_path / "data" / "run_T_orph_xhs.pid"
    pf.write_text("99999")
    killed = []
    updates = {}
    monkeypatch.setattr(orchestrator, "_is_python_pid", lambda pid: True)
    monkeypatch.setattr(orchestrator, "_kill_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(orchestrator.task_store, "update_task",
                        lambda tid, d: updates.update(d))
    monkeypatch.setattr(orchestrator.task_store, "update_platform_status", lambda *a: None)
    assert orchestrator.stop_orphan("T_orph") is True
    assert killed == [99999]
    assert updates["status"] == orchestrator.task_store.STATUS_STOPPED
    assert not pf.exists()


def test_stop_orphan_skips_non_python_pid(tmp_path, monkeypatch):
    # PID 复用保护：进程已不是 python 则不杀，仅清理 pid 文件
    monkeypatch.setattr(orchestrator, "ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    pf = tmp_path / "data" / "run_T_orph_xhs.pid"
    pf.write_text("88888")
    killed = []
    monkeypatch.setattr(orchestrator, "_is_python_pid", lambda pid: False)
    monkeypatch.setattr(orchestrator, "_kill_pid", lambda pid: killed.append(pid))
    assert orchestrator.stop_orphan("T_orph") is False
    assert killed == []
    assert not pf.exists()


def test_run_platform_cleans_pid_file(orch_env, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(config, "PLATFORM_MAX_RUNTIME_MIN", 0.001)
    orchestrator.run_platform(TASK, "xhs", *WIN)
    assert not (tmp_path / "data" / "run_T_stop_xhs.pid").exists()


def test_stop_api_orphan_fallback(tmp_path, monkeypatch):
    import server
    from tools import task_store, login_state

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "QR_STATE", tmp_path / "qrcode_state.json")
    monkeypatch.setattr(login_state, "STATE_FILE", str(tmp_path / "login_state.json"))
    monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
    (tmp_path / "tasks.json").write_text(
        json.dumps([{"id": "T_o", "name": "o", "status": "running",
                     "schedule_type": "immediate", "platforms": ["xhs"]}]),
        encoding="utf-8")
    called = []
    monkeypatch.setattr(server.orchestrator, "stop_orphan",
                        lambda tid: called.append(tid) or True)
    with TestClient(server.app) as c:
        r = c.post("/api/tasks/T_o/stop")
        assert r.status_code == 200
        assert r.json()["message"] == "已停止"
    assert called == ["T_o"]