# -*- coding: utf-8 -*-
# 两段式阅读停留单测（方案A）：扫读必有、30% 细读、真人动作、
# 评论顺带缓存不丢、风控异常上抛、所有等待两位小数。
import asyncio

import pytest

from media_platform.xhs import ui_bridge
from media_platform.xhs.ui_bridge import XhsUIBridge
from media_platform.xhs.exception import RiskControlError


class FakeMouse:
    def __init__(self):
        self.wheels = []
        self.moves = []

    async def wheel(self, dx, dy):
        self.wheels.append((dx, dy))

    async def move(self, x, y, steps=1):
        self.moves.append((x, y, steps))


class FakePage:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self):
        self.mouse = FakeMouse()


class FakeRhythm:
    def __init__(self):
        self.actions = 0
        self.browses = 0

    async def human_action(self):
        self.actions += 1

    async def browse_step(self):
        self.browses += 1


def _bridge(monkeypatch, rand_value):
    b = object.__new__(XhsUIBridge)
    b.page = FakePage()
    b._rhythm = FakeRhythm()
    b._comment_queue = asyncio.Queue()
    b._comment_cache = {}
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(ui_bridge.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ui_bridge.random, "random", lambda: rand_value)
    monkeypatch.setattr(ui_bridge.random, "uniform", lambda a, b: round((a + b) / 2, 2))
    monkeypatch.setattr(ui_bridge.random, "randint", lambda a, b: a)
    return b, sleeps


@pytest.mark.asyncio
async def test_skim_only_branch(monkeypatch):
    # random()=0.9 → 不细读：只有扫读（1 次鼠标动作 + 1 次滚动），无 browse_step
    b, sleeps = _bridge(monkeypatch, 0.9)
    await b._read_dwell({"title": "t" * 10, "desc": "d" * 300}, "n1")
    assert b._rhythm.actions == 1
    assert b._rhythm.browses == 0
    assert len(b.page.mouse.wheels) == 1
    assert sleeps and all(round(s, 2) == s for s in sleeps)
    assert all(0 < s <= 3.5 for s in sleeps)  # 扫读总时长 1.8–3.5s


@pytest.mark.asyncio
async def test_deep_read_branch_with_human_actions(monkeypatch):
    # random()=0.0 → 细读 + 中途多停 + 看图悬停 + 末段提前离开
    b, sleeps = _bridge(monkeypatch, 0.0)
    await b._read_dwell({"title": "t" * 10, "desc": "d" * 350}, "n2")
    assert b._rhythm.browses >= 1          # 细读分段滚动
    assert len(b.page.mouse.moves) >= 1    # 看图悬停
    assert all(round(s, 2) == s for s in sleeps)
    # 细读单段停留不超过封顶 12s
    assert max(sleeps) <= 12.0


@pytest.mark.asyncio
async def test_comments_cached_during_dwell(monkeypatch):
    b, _ = _bridge(monkeypatch, 0.9)
    await b._comment_queue.put((200, {}, {"cid": "c1"}))

    async def fake_check(status, headers, data):
        return data

    b._check = fake_check
    await b._read_dwell({"title": "t", "desc": "d"}, "n3")
    assert b._comment_cache.get("n3") == [{"cid": "c1"}]


@pytest.mark.asyncio
async def test_risk_error_propagates(monkeypatch):
    b, _ = _bridge(monkeypatch, 0.9)
    await b._comment_queue.put((429, {}, {}))

    async def fake_check(status, headers, data):
        raise RiskControlError("rate limited")

    b._check = fake_check
    with pytest.raises(RiskControlError):
        await b._read_dwell({"title": "t", "desc": "d"}, "n4")