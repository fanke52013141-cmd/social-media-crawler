# -*- coding: utf-8 -*-
# 等待时长精度单测：所有拟人化等待保留两位小数（非整数秒），且在区间内。
from tools.humanize import _human_wait_seconds


def test_wait_two_decimals_in_range():
    for _ in range(300):
        v = _human_wait_seconds(2, 5)
        assert 2 <= v <= 5
        assert round(v, 2) == v


def test_wait_session_rest_two_decimals():
    for _ in range(100):
        v = _human_wait_seconds(30, 90)
        assert 30 <= v <= 90
        assert round(v, 2) == v