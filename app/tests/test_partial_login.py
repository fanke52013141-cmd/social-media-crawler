# -*- coding: utf-8 -*-
# 批5单元测试：部分平台登录失效跳过继续 / 全部失效才整体失败 / 游客免体检 / 任务列表最新置顶。
import pytest

import server
from tools import task_store, orchestrator, task_exporter, collect_metrics
from tools.scheduler import Scheduler


@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
    return tmp_path


def _payload(**kw):
    base = {"platforms": ["wb", "tieba"], "keywords": "测试", "crawl_mode": "login",
            "data_mode": "content", "input_type": "keyword",
            "schedule_type": "immediate"}
    base.update(kw)
    return base


@pytest.fixture
def exec_mocks(monkeypatch):
    """屏蔽真实采集/导出/指标写入，捕获 run_once 收到的平台子集。"""
    captured = {}

    def fake_run_once(task, win_start, win_end, deadline_ts=None, platforms=None):
        captured["platforms"] = platforms
        return {"stopped": False,
                "results": {p: {"status": "done", "contents": 1, "comments": 0}
                            for p in (platforms if platforms is not None else task["platforms"])},
                "win_start": win_start, "win_end": win_end}

    monkeypatch.setattr(orchestrator, "run_once", fake_run_once)
    monkeypatch.setattr(task_exporter, "export_task", lambda *a, **k: "")
    monkeypatch.setattr(collect_metrics, "evaluate",
                        lambda plat, c: {"level": "ok", "message": ""})
    monkeypatch.setattr(collect_metrics, "record_run", lambda *a, **k: None)
    return captured


class TestPartialLogin:
    def test_partial_invalid_skips_and_continues(self, store_tmp, exec_mocks, monkeypatch):
        t = task_store.create_task(_payload())
        monkeypatch.setattr(Scheduler, "_preflight_login",
                            lambda self, task: {"wb": True, "tieba": False})
        out = Scheduler().execute_once(t, "2026-11-12 00:00:00", "2026-11-12 23:59:59", None)
        assert out["status"] == task_store.STATUS_DONE
        assert out["blocked"] is False
        assert out["skipped"] == ["tieba"]
        assert exec_mocks["platforms"] == ["wb"]  # 失效平台被剔除，其余继续
        cur = task_store.get_task(t["id"])
        assert cur["platform_status"]["tieba"]["status"] == "failed"
        assert "跳过" in cur["platform_status"]["tieba"]["message"]

    def test_all_invalid_fails_whole_task(self, store_tmp, exec_mocks, monkeypatch):
        t = task_store.create_task(_payload())
        monkeypatch.setattr(Scheduler, "_preflight_login",
                            lambda self, task: {"wb": False, "tieba": False})
        out = Scheduler().execute_once(t, "2026-11-12 00:00:00", "2026-11-12 23:59:59", None)
        assert out["status"] == task_store.STATUS_FAILED
        assert out["blocked"] is True
        assert "platforms" not in exec_mocks  # 一个平台都不采

    def test_guest_mode_runs_all_without_preflight(self, store_tmp, exec_mocks):
        # 游客免体检逻辑在 _preflight_login 内部（crawl_mode != login 直接返回空），
        # 这里用真实函数：不启浏览器、不拦平台，全部平台照常执行。
        t = task_store.create_task(_payload(crawl_mode="guest"))
        out = Scheduler().execute_once(t, "2026-11-12 00:00:00", "2026-11-12 23:59:59", None)
        assert out["status"] == task_store.STATUS_DONE
        assert out["skipped"] == []
        assert exec_mocks["platforms"] == ["wb", "tieba"]


class TestTaskOrder:
    def test_task_list_newest_first(self, store_tmp):
        t1 = task_store.create_task(_payload(name="旧任务"))
        t2 = task_store.create_task(_payload(name="中任务"))
        t3 = task_store.create_task(_payload(name="新任务"))
        # created_at 精度到秒，显式拉开时间差保证排序确定
        task_store.update_task(t1["id"], {"created_at": "2026-11-10T09:00:00"})
        task_store.update_task(t2["id"], {"created_at": "2026-11-11T09:00:00"})
        task_store.update_task(t3["id"], {"created_at": "2026-11-12T09:00:00"})
        listed = server.get_tasks()["tasks"]
        assert [t["id"] for t in listed] == [t3["id"], t2["id"], t1["id"]]


class TestExportUnmapped:
    def test_export_with_unmapped_platform_does_not_block(self, store_tmp, monkeypatch):
        import json as _json
        from datetime import datetime as _dt
        from openpyxl import load_workbook

        monkeypatch.chdir(store_tmp)
        jdir = store_tmp / "data" / "weibo" / "jsonl"
        jdir.mkdir(parents=True)
        ts = int(_dt(2026, 11, 12, 10, 0, 0).timestamp())
        row = {"note_id": "n1", "content": "帖子", "create_time": ts,
               "nickname": "用户", "note_url": "https://weibo.com/n1",
               "source_keyword": "测试"}
        (jdir / "search_contents_2026-11-12.jsonl").write_text(
            _json.dumps(row, ensure_ascii=False), encoding="utf-8")

        t = task_store.create_task(_payload(platforms=["wb", "tieba"],
                                            time_start="2026-11-12 00:00:00",
                                            time_end="2026-11-12 23:59:59"))
        xlsx = task_exporter.export_task(t["id"], t["time_start"], t["time_end"])
        wb = load_workbook(xlsx, read_only=True)
        assert "微博" in wb.sheetnames
        assert "tieba" in wb.sheetnames  # 未适配平台写提示 sheet，不阻断
        ws = wb["微博"]
        rows = list(ws.iter_rows(values_only=True))
        assert len(rows) - 1 == 1  # 已适配平台正常导出
        wb.close()