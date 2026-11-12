# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/exception.py
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


from httpx import RequestError


class DataFetchError(RequestError):
    """something error when fetch"""


class IPBlockError(RequestError):
    """fetch so fast that the server block us ip"""


class PlatformAccessError(RequestError):
    """authentication, rate-limit, or account-security restriction"""


class RateLimitError(RequestError):
    """temporary rate limiting (HTTP 429 / security limit), safe to retry after backoff"""


class RiskControlError(RequestError):
    """平台风控已介入（429/300011/验证码）：立即收手，不做秒级硬重试。

    账号已被警告的背景下，风控窗口内反复重试会把限制升级为封禁。
    该异常不参与 tenacity 重试，直接终止本轮采集，由编排器施加阶梯冷却。
    """


class CaptchaError(RequestError):
    """human verification required (HTTP 461/471), needs headed browser or manual solve"""


class NoteNotFoundError(RequestError):
    """Note does not exist or is abnormal"""
