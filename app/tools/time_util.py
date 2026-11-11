# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/time_util.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 12:52
# @Desc    : Time utility functions

import time
from datetime import datetime, timedelta, timezone


def get_current_timestamp() -> int:
    """
    Get current timestamp (13 digits): 1701493264496
    :return:
    """
    return int(time.time() * 1000)


def get_current_time() -> str:
    """
    Get current time: '2023-12-02 13:01:23'
    :return:
    """
    return time.strftime('%Y-%m-%d %X', time.localtime())

def get_current_time_hour() -> str:
    """
    Get current time with hour: '2023-12-02-13'
    :return:
    """
    return time.strftime('%Y-%m-%d-%H', time.localtime())

def get_current_date() -> str:
    """
    Get current date: '2023-12-02'
    :return:
    """
    return time.strftime('%Y-%m-%d', time.localtime())


def get_time_str_from_unix_time(unixtime):
    """
    Unix integer timestamp ==> datetime string
    :param unixtime:
    :return:
    """
    if int(unixtime) > 1000000000000:
        unixtime = int(unixtime) / 1000
    return time.strftime('%Y-%m-%d %X', time.localtime(unixtime))


def get_date_str_from_unix_time(unixtime):
    """
    Unix integer timestamp ==> date string
    :param unixtime:
    :return:
    """
    if int(unixtime) > 1000000000000:
        unixtime = int(unixtime) / 1000
    return time.strftime('%Y-%m-%d', time.localtime(unixtime))


def get_unix_time_from_time_str(time_str):
    """
    Time string ==> Unix integer timestamp, precise to seconds
    :param time_str:
    :return:
    """
    try:
        format_str = "%Y-%m-%d %H:%M:%S"
        tm_object = time.strptime(str(time_str), format_str)
        return int(time.mktime(tm_object))
    except Exception as e:
        return 0
    pass


def get_unix_timestamp():
    return int(time.time())


def rfc2822_to_china_datetime(rfc2822_time):
    # Define RFC 2822 format
    rfc2822_format = "%a %b %d %H:%M:%S %z %Y"

    # Convert RFC 2822 time string to datetime object
    dt_object = datetime.strptime(rfc2822_time, rfc2822_format)

    # Convert datetime object timezone to China timezone
    dt_object_china = dt_object.astimezone(timezone(timedelta(hours=8)))
    return dt_object_china


def rfc2822_to_timestamp(rfc2822_time):
    # Define RFC 2822 format
    rfc2822_format = "%a %b %d %H:%M:%S %z %Y"

    # Convert RFC 2822 time string to datetime object
    dt_object = datetime.strptime(rfc2822_time, rfc2822_format)

    # Convert datetime object to UTC time
    dt_utc = dt_object.astimezone(timezone.utc)

    # Calculate Unix timestamp from UTC time
    timestamp = int(dt_utc.timestamp())

    return timestamp


def iso_to_ts(iso: str):
    """ISO 本地时间字符串（YYYY-MM-DDTHH:MM:SS）转 Unix 秒级时间戳。"""
    if not iso:
        return None
    s = iso.strip().replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(datetime.strptime(s, fmt).timetuple()))
        except ValueError:
            continue
    return None


def to_seconds(ts) -> int:
    """时间戳统一转秒：13 位毫秒（>1e12）除以 1000，10 位秒级原样返回。

    不同平台时间字段单位不一（如小红书 time 为毫秒、抖音 create_time 为秒），
    统一入口避免拿毫秒与秒级窗口边界比较导致误判。
    """
    t = int(float(ts))
    if t > 1000000000000:
        t = t // 1000
    return t


def window_start_ts():
    import config
    return iso_to_ts(getattr(config, "TIME_START", ""))


def window_end_ts():
    import config
    return iso_to_ts(getattr(config, "TIME_END", ""))


def in_window(ts) -> bool:
    """判断时间戳（秒或毫秒）是否落在配置的时间窗口内（含端点）。"""
    try:
        t = to_seconds(ts)
    except (TypeError, ValueError):
        return True
    lo, hi = window_start_ts(), window_end_ts()
    if lo is not None and t < lo:
        return False
    if hi is not None and t > hi:
        return False
    return True


def before_window_start(ts) -> bool:
    """时间戳是否早于窗口起始时间（按时间排序时可提前停止翻页）。"""
    lo = window_start_ts()
    if lo is None:
        return False
    try:
        return to_seconds(ts) < lo
    except (TypeError, ValueError):
        return False


if __name__ == '__main__':
    # Example usage
    _rfc2822_time = "Sat Dec 23 17:12:54 +0800 2023"
    print(rfc2822_to_china_datetime(_rfc2822_time))
