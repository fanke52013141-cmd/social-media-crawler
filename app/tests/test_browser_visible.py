# -*- coding: utf-8 -*-
# 批6单元测试：浏览器窗口可见开关（默认静默离屏 / 打开后窗口可见 / 设置持久化）。
import pytest

from tools import settings, browser_launch


@pytest.fixture
def settings_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    return tmp_path


def test_default_silent_offscreen(settings_tmp):
    assert settings.browser_visible() is False
    args = browser_launch.offscreen_args("xhs")
    assert "--window-position=-32000,-32000" in args
    # 非重点平台原本就不离屏
    assert browser_launch.offscreen_args("unknown_plat") == []


def test_visible_mode_removes_offscreen(settings_tmp):
    settings.save({"browser_visible": True})
    assert settings.browser_visible() is True
    for plat in ("xhs", "dy", "wb", "toutiao", "ks", "bili", "tieba", "zhihu"):
        assert browser_launch.offscreen_args(plat) == []
    # 可见模式仍是有头（完整浏览器），只是窗口留在屏幕内
    assert browser_launch.effective_headless("xhs", True) is False


def test_save_roundtrip_ignores_unknown_keys(settings_tmp):
    settings.save({"browser_visible": True, "bogus": 1})
    assert settings.load() == {"browser_visible": True}
    settings.save({"browser_visible": False})
    assert settings.browser_visible() is False
    assert settings.load() == {"browser_visible": False}


def test_corrupt_settings_falls_back_to_default(settings_tmp):
    (settings_tmp / "settings.json").write_text("{not json", encoding="utf-8")
    assert settings.load() == {"browser_visible": False}