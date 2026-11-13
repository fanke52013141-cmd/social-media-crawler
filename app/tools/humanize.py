# -*- coding: utf-8 -*-
"""公共拟人化节奏：真人鼠标动作 + 随机等待 + 长尾停顿 + 会话休息。

把小红书已验证的拟人化逻辑下沉为三平台（小红书/抖音/微博）统一能力，
替代固定、均匀的 sleep，降低请求节奏被风控模型归纳为机器特征的概率。
设计原则与安全保守模式一致：不改变最终数据量，只让过程更慢、更无规律；
任何页面异常都整体容错，绝不中断采集主流程。
"""
import asyncio
import math
import random
from typing import Optional

from playwright.async_api import Page

import config
from tools import utils


def _human_wait_seconds(lo: float, hi: float) -> float:
    """生成更接近真人注意力的等待时长：对数正态分布（多数偏短、少数偏长），
    再截断到 [lo, hi]，比均匀分布更像真人的节奏。
    保留两位小数（如 6.32s），避免整数秒的机器特征。"""
    median = max(0.05, (lo + hi) / 2.0)
    val = median * math.exp(random.gauss(0.0, 0.55))
    return round(min(hi, max(lo, val)), 2)


class HumanRhythm:
    """绑定一个 Playwright 页面，提供拟人化动作与等待。"""

    def __init__(self, page: Optional[Page] = None) -> None:
        self.page = page
        # 会话动作计数：累计到阈值后强制休息，歇完继续（不截断任务）
        self._session_actions = 0

    def bind(self, page: Page) -> None:
        """页面变化时重新绑定（如登录后新建/跳转的页面）。"""
        self.page = page

    async def human_action(self) -> None:
        """在页面上模拟真人的鼠标移动与小幅滚动，让浏览轨迹更像人。"""
        if not getattr(config, "HUMAN_BEHAVIOR", False) or self.page is None:
            return
        try:
            page = self.page
            vp = page.viewport_size or {"width": 1280, "height": 800}
            w, h = vp.get("width", 1280), vp.get("height", 800)
            from tools.human_mouse import move_human
            # 两段弧线移动（带过冲回正），替代直线插值，更像真人手部轨迹
            sx, sy = random.randint(0, w), random.randint(0, h)
            mx, my = random.randint(0, w), random.randint(0, h)
            x = random.randint(int(w * 0.15), int(w * 0.85))
            y = random.randint(int(h * 0.15), int(h * 0.8))
            await move_human(page, sx, sy, mx, my)
            await move_human(page, mx, my, x, y)
            # 偶尔小幅滚动页面
            if random.random() < 0.4:
                await page.mouse.wheel(0, random.randint(120, 600))
        except Exception:
            return

    async def browse_step(self) -> None:
        """模拟一段真实浏览：分 1–3 段向下滚动阅读，段间短暂停留，
        并有 25% 概率往回滚一点（真人常回看），整体容错。"""
        if not getattr(config, "HUMAN_BEHAVIOR", False) or self.page is None:
            return
        try:
            for _ in range(random.randint(1, 3)):
                await self.page.mouse.wheel(0, random.randint(200, 700))
                await asyncio.sleep(_human_wait_seconds(0.4, 1.6))
            if random.random() < 0.25:
                await self.page.mouse.wheel(0, -random.randint(150, 400))
        except Exception:
            return

    async def human_pause(self, lo: Optional[float] = None,
                          hi: Optional[float] = None) -> None:
        """拟人化等待：先做真人动作，再随机等待（含概率长尾），累计动作触发会话休息。"""
        if lo is None:
            lo = config.CRAWLER_SLEEP_MIN
        if hi is None:
            hi = config.CRAWLER_SLEEP_MAX
        # 等待前先做一个真人动作：约 30% 概率走一段浏览，其余做鼠标动作
        if random.random() < 0.3:
            await self.browse_step()
        else:
            await self.human_action()
        # 长尾分布：小概率切换为长停顿，模拟真人走神或阅读
        if getattr(config, "SAFE_MODE", False) and random.random() < getattr(
            config, "LONG_TAIL_PAUSE_PROB", 0
        ):
            wait_s = _human_wait_seconds(config.LONG_PAUSE_MIN, config.LONG_PAUSE_MAX)
        else:
            wait_s = _human_wait_seconds(lo, hi)
        await asyncio.sleep(wait_s)
        # 会话制：累计动作达到阈值，强制休息一次，然后继续
        if getattr(config, "SAFE_MODE", False):
            self._session_actions += 1
            if self._session_actions >= getattr(config, "SESSION_ACTION_COUNT", 10**9):
                self._session_actions = 0
                rest = _human_wait_seconds(config.SESSION_REST_MIN, config.SESSION_REST_MAX)
                utils.logger.info(
                    f"[HumanRhythm] 会话休息 {rest:.2f}s 后继续（不影响最终数据）"
                )
                await asyncio.sleep(rest)

    async def random_sleep(self, lo: Optional[float] = None,
                           hi: Optional[float] = None) -> None:
        """普通接口请求后的拟人化等待（通用入口）。"""
        await self.human_pause(lo, hi)
