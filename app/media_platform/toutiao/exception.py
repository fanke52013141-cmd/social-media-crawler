# -*- coding: utf-8 -*-
class ToutiaoError(Exception):
    """今日头条采集基础异常"""


class DataFetchError(ToutiaoError):
    """页面数据获取失败"""


class CaptchaError(ToutiaoError):
    """触发验证码/风控拦截"""
