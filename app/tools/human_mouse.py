# -*- coding: utf-8 -*-
# 拟人化鼠标：曲线移动（带过冲回正）+ 拟人点击（位置抖动/接近轨迹/按压时长抖动）。
# 设计目标：补齐"点击永远点正中心、瞬间到位"的机器特征。真人点击有落点偏移、
# 弧线接近、偶尔过冲回正、按压与犹豫时长不一。所有页面异常整体容错，回退普通点击，
# 绝不中断采集主流程。受 config.HUMAN_BEHAVIOR 开关控制。
import asyncio
import math
import random
from typing import Dict, List, Optional, Tuple

import config


def click_point(box: Dict[str, float]) -> Tuple[float, float]:
    """在元素包围盒的中心 60% 区域（20%–80%）内取随机落点，避免每次精准点正中心。"""
    x, y = box["x"], box["y"]
    w, h = box["width"], box["height"]
    return (x + w * random.uniform(0.2, 0.8),
            y + h * random.uniform(0.2, 0.8))


def curve_points(x0: float, y0: float, x1: float, y1: float,
                 steps: Optional[int] = None, overshoot: bool = True) -> List[Tuple[float, float]]:
    """生成从 (x0,y0) 到 (x1,y1) 的三次贝塞尔弧线轨迹，带随机弯曲；
    距离足够时附加轻微过冲（冲过目标再回正），模拟真人手部动作。

    纯函数，便于单测：首尾点精确、轨迹非直线、过冲可验证。
    """
    if steps is None:
        steps = random.randint(18, 30)
    n = max(2, int(steps))
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return [(float(x0), float(y0)), (float(x1), float(y1))]
    # 垂直于运动方向的单位向量，用于制造弧线弯曲
    px, py = -dy / dist, dx / dist
    bow = dist * random.uniform(0.08, 0.22) * random.choice([-1, 1])
    c1x = x0 + dx / 3 + px * bow
    c1y = y0 + dy / 3 + py * bow
    c2x = x0 + 2 * dx / 3 + px * bow * random.uniform(0.4, 1.0)
    c2y = y0 + 2 * dy / 3 + py * bow * random.uniform(0.4, 1.0)
    pts: List[Tuple[float, float]] = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt ** 3 * x0 + 3 * mt ** 2 * t * c1x + 3 * mt * t ** 2 * c2x + t ** 3 * x1
        y = mt ** 3 * y0 + 3 * mt ** 2 * t * c1y + 3 * mt * t ** 2 * c2y + t ** 3 * y1
        pts.append((x, y))
    # 过冲：沿运动方向冲过目标一小段，再分几步回正
    if overshoot and dist > 40:
        over = dist * random.uniform(0.03, 0.08)
        ox = x1 + dx / dist * over
        oy = y1 + dy / dist * over
        pts.append((ox, oy))
        back = random.randint(2, 3)
        for k in range(1, back + 1):
            pts.append((ox + (x1 - ox) * k / back, oy + (y1 - oy) * k / back))
    pts[0] = (float(x0), float(y0))
    pts[-1] = (float(x1), float(y1))
    return pts


async def move_human(page, x0: float, y0: float, x1: float, y1: float,
                     steps: Optional[int] = None) -> None:
    """沿弧线轨迹移动鼠标（逐点移动形成曲线），偶有微小停顿。"""
    for (x, y) in curve_points(x0, y0, x1, y1, steps=steps):
        await page.mouse.move(x, y)
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(0.005, 0.02))


async def click_human(page, locator, timeout: int = 5000) -> None:
    """拟人化点击：随机落点 + 弧线接近 + 犹豫停顿 + 随机按压时长。

    HUMAN_BEHAVIOR 关闭、取不到包围盒或任何异常时，回退普通 locator.click()，
    保证不因拟人化失败而中断采集。
    """
    if not getattr(config, "HUMAN_BEHAVIOR", False):
        await locator.click(timeout=timeout)
        return
    try:
        box = await locator.bounding_box()
        if not box or box.get("width", 0) < 4 or box.get("height", 0) < 4:
            await locator.click(timeout=timeout)
            return
        tx, ty = click_point(box)
        # 从目标附近的随机点弧线接近
        sx = tx + random.uniform(-120, 120)
        sy = ty + random.uniform(-90, 90)
        await move_human(page, sx, sy, tx, ty)
        await asyncio.sleep(round(random.uniform(0.1, 0.4), 2))   # 点击前犹豫
        await page.mouse.move(tx, ty)
        await page.mouse.down()
        await asyncio.sleep(round(random.uniform(0.05, 0.15), 2))  # 按压时长抖动
        await page.mouse.up()
    except Exception:
        await locator.click(timeout=timeout)