# -*- coding: utf-8 -*-
# 退出登录/换号接口测试：隔离备份不删除、重置登录态、运行中禁止退出。
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    import server
    from tools import login_state, task_store

    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "QR_STATE", tmp_path / "qrcode_state.json")
    monkeypatch.setattr(login_state, "STATE_FILE", str(tmp_path / "login_state.json"))
    monkeypatch.setattr(task_store, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(task_store, "TASK_FILE", str(tmp_path / "tasks.json"))
    (tmp_path / "browser_data" / "xhs_user_data_dir").mkdir(parents=True)
    server.state["proc"] = None
    with TestClient(server.app) as c:
        yield c, tmp_path


class TestLogout:
    def test_logout_backs_up_profile_and_resets_state(self, env):
        c, tmp = env
        r = c.post("/api/logout", json={"platforms": ["xhs"]})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["profile_backed_up"] == ["xhs"]
        assert not (tmp / "browser_data" / "xhs_user_data_dir").exists()
        baks = list((tmp / "browser_data").glob("xhs_user_data_dir_bak_*"))
        assert len(baks) == 1  # 隔离备份，不删除
        from tools import login_state
        assert login_state.read_state()["xhs"]["logged_in"] is False

    def test_logout_single_platform_field(self, env):
        c, tmp = env
        r = c.post("/api/logout", json={"platform": "xhs"})
        assert r.status_code == 200
        assert r.json()["logged_out"] == ["xhs"]

    def test_logout_rejected_while_running(self, env):
        c, tmp = env
        import server

        class Running:
            def poll(self):
                return None
        server.state["proc"] = Running()
        r = c.post("/api/logout", json={"platforms": ["xhs"]})
        assert r.status_code == 409
        assert (tmp / "browser_data" / "xhs_user_data_dir").exists()

    def test_logout_bad_platform(self, env):
        c, _ = env
        assert c.post("/api/logout", json={"platforms": ["nope"]}).status_code == 400

    def test_logout_without_profile_still_resets(self, env):
        c, tmp = env
        import shutil
        shutil.rmtree(tmp / "browser_data" / "xhs_user_data_dir")
        r = c.post("/api/logout", json={"platforms": ["xhs"]})
        assert r.status_code == 200
        assert r.json()["profile_backed_up"] == []