# -*- coding: utf-8 -*-
# 批8单元测试：阶梯冷却升级 / 高危信号口径 / curl_cffi 浏览器 TLS 指纹通道。
import time

import pytest

from tools import risk_signals
from tools import tls_client


@pytest.fixture
def risk_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_signals, "COOLDOWN_FILE", str(tmp_path / "risk_cooldown.json"))
    monkeypatch.setattr(risk_signals, "EVENT_FILE", str(tmp_path / "risk_events.jsonl"))
    return tmp_path


class TestEscalatingCooldown:
    def test_tiers_escalate_within_24h(self, risk_tmp):
        assert risk_signals.escalating_cooldown("xhs", "first") == 1800    # 30 分钟
        assert risk_signals.in_cooldown("xhs") is True
        assert risk_signals.escalating_cooldown("xhs", "second") == 7200   # 2 小时
        assert risk_signals.escalating_cooldown("xhs", "third") == 86400   # 24 小时
        assert risk_signals.escalating_cooldown("xhs", "fourth") == 86400  # 封顶

    def test_history_pruned_after_24h(self, risk_tmp):
        risk_signals.escalating_cooldown("xhs", "old")
        # 把历史改成 25 小时前 → 再犯回到首档
        data = risk_signals._read_cooldown()
        data["xhs"]["history"] = [round(time.time() - 25 * 3600, 1)]
        risk_signals._write_cooldown(data)
        assert risk_signals.escalating_cooldown("xhs", "new") == 1800

    def test_platforms_isolated(self, risk_tmp):
        risk_signals.escalating_cooldown("xhs", "a")
        risk_signals.escalating_cooldown("xhs", "b")
        assert risk_signals.escalating_cooldown("dy", "a") == 1800  # 不受 xhs 再犯影响

    def test_high_risk_kinds_include_rate_limit(self):
        assert risk_signals.KIND_RATE_LIMIT in risk_signals.HIGH_RISK_KINDS
        assert risk_signals.KIND_SECURITY in risk_signals.HIGH_RISK_KINDS
        assert risk_signals.KIND_CAPTCHA in risk_signals.HIGH_RISK_KINDS
        assert risk_signals.HIGH_RISK_THRESHOLD == 1  # 首次信号即收手


class TestTlsClient:
    def test_impersonates_real_browser(self):
        from curl_cffi.requests import AsyncSession
        s = tls_client.make_tls_client()
        assert isinstance(s, AsyncSession)
        assert s.impersonate == "chrome"  # 浏览器 TLS/JA3 指纹，而非 Python httpx

    def test_verify_default_on(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "DISABLE_SSL_VERIFY", False, raising=False)
        s = tls_client.make_tls_client()
        assert s.verify is True