# -*- coding: utf-8 -*-
# 拟人化鼠标单测：落点抖动分布 / 曲线非直线 / 过冲回正 / 点击容错回退。
import pytest

import config
from tools import human_mouse


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.downs = 0
        self.ups = 0

    async def move(self, x, y, **kw):
        self.moves.append((x, y))

    async def down(self):
        self.downs += 1

    async def up(self):
        self.ups += 1


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.viewport_size = {"width": 1280, "height": 800}


class FakeLocator:
    def __init__(self, box=None):
        self._box = box
        self.clicks = 0

    async def bounding_box(self):
        return self._box

    async def click(self, timeout=None):
        self.clicks += 1


class TestClickPoint:
    def test_within_central_region(self):
        box = {"x": 100, "y": 200, "width": 100, "height": 50}
        for _ in range(200):
            x, y = human_mouse.click_point(box)
            assert 120 <= x <= 180   # 中心 60%（20%–80%）
            assert 210 <= y <= 240

    def test_varies_not_fixed_center(self):
        box = {"x": 0, "y": 0, "width": 100, "height": 100}
        pts = {human_mouse.click_point(box) for _ in range(50)}
        assert len(pts) > 10  # 随机落点，而非每次正中心


class TestCurvePoints:
    def test_endpoints_exact(self):
        pts = human_mouse.curve_points(10, 20, 300, 400, steps=20)
        assert pts[0] == (10.0, 20.0)
        assert pts[-1] == (300.0, 400.0)

    def test_not_straight(self):
        pts = human_mouse.curve_points(0, 0, 100, 0, steps=30, overshoot=False)
        dev = max(abs(y) for x, y in pts)  # 直线应为 y=0
        assert dev > 1.0  # 有弧度

    def test_overshoot_then_correct(self):
        pts = human_mouse.curve_points(0, 0, 100, 0, steps=20, overshoot=True)
        assert max(x for x, y in pts) > 100.0  # 冲过目标
        assert pts[-1] == (100.0, 0.0)         # 回正到目标

    def test_no_overshoot_when_disabled(self):
        pts = human_mouse.curve_points(0, 0, 100, 0, steps=20, overshoot=False)
        assert max(x for x, y in pts) <= 100.0 + 1e-6

    def test_degenerate_same_point(self):
        pts = human_mouse.curve_points(5, 5, 5, 5)
        assert pts[-1] == (5.0, 5.0)


class TestMoveHuman:
    @pytest.mark.asyncio
    async def test_multiple_steps_along_curve(self):
        page = FakePage()
        await human_mouse.move_human(page, 0, 0, 200, 100)
        assert len(page.mouse.moves) >= 10
        assert page.mouse.moves[-1] == (200.0, 100.0)


class TestClickHuman:
    @pytest.mark.asyncio
    async def test_disabled_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "HUMAN_BEHAVIOR", False)
        page = FakePage()
        loc = FakeLocator(box={"x": 0, "y": 0, "width": 50, "height": 50})
        await human_mouse.click_human(page, loc)
        assert loc.clicks == 1
        assert page.mouse.downs == 0

    @pytest.mark.asyncio
    async def test_no_box_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "HUMAN_BEHAVIOR", True)
        page = FakePage()
        loc = FakeLocator(box=None)
        await human_mouse.click_human(page, loc)
        assert loc.clicks == 1

    @pytest.mark.asyncio
    async def test_humanized_path(self, monkeypatch):
        monkeypatch.setattr(config, "HUMAN_BEHAVIOR", True)
        page = FakePage()
        loc = FakeLocator(box={"x": 100, "y": 100, "width": 80, "height": 40})
        await human_mouse.click_human(page, loc)
        assert page.mouse.downs == 1
        assert page.mouse.ups == 1
        assert len(page.mouse.moves) >= 10  # 弧线接近轨迹
        assert loc.clicks == 0              # 走拟人路径，非回退

    @pytest.mark.asyncio
    async def test_exception_falls_back(self, monkeypatch):
        monkeypatch.setattr(config, "HUMAN_BEHAVIOR", True)
        page = FakePage()

        class BadLoc:
            clicks = 0

            async def bounding_box(self):
                raise RuntimeError("boom")

            async def click(self, timeout=None):
                self.clicks += 1

        loc = BadLoc()
        await human_mouse.click_human(page, loc)
        assert loc.clicks == 1  # 异常时回退普通点击，不中断