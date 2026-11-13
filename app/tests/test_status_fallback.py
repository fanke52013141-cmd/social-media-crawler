# -*- coding: utf-8 -*-
# 服务重启后进度可见性：调度器内存态丢失时，/api/status 回退到
# tasks.json 中仍运行的任务，读取其编排日志重建进度（孤儿子进程场景）。
import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server
    from tools import task_store, login_state

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "LOG", tmp_path / "server_run.log")
    monkeypatch.setattr(server, "QR_STATE", tmp_path / "qrcode_state.json")
    monkeypatch.setattr(login_state, "STATE_FILE", str(tmp_path / "login_state.json"))
    monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
    server.state["proc"] = None
    with TestClient(server.app) as c:
        yield c, tmp_path


def _write_task(tmp, tid, status="running"):
    task = {
        "id": tid, "name": "测试任务", "status": status,
        "schedule_type": "immediate", "platforms": ["xhs"],
        "current_platform": "xhs", "data_mode": "content", "crawl_mode": "login",
        "platform_status": {"xhs": {"started_ts": time.time() - 600}},
    }
    (tmp / "tasks.json").write_text(json.dumps([task]), encoding="utf-8")
    return task


def test_status_fallback_to_running_task(env):
    c, tmp = env
    tid = "T_test_fallback"
    _write_task(tmp, tid)
    log = tmp / f"orchestrator_{tid}_xhs.log"
    log.write_text(
        "2026-11-13 10:00:01 MediaCrawler INFO - [get_note_detail_async_task] "
        "Begin get note detail, note_id: aaa\n"
        "2026-11-13 10:00:02 MediaCrawler INFO - [get_note_detail_async_task] "
        "Begin get note detail, note_id: bbb\n",
        encoding="utf-8")
    r = c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is True
    assert body["task_id"] == tid
    assert body["progress"]["counts"]["viewed"] == 2


def test_status_no_fallback_when_finished(env):
    c, tmp = env
    _write_task(tmp, "T_done", status="done")
    r = c.get("/api/status")
    assert r.status_code == 200
    assert r.json()["task_id"] == ""