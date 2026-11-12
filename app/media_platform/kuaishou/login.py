# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/kuaishou/login.py
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


import asyncio
import sys
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import (RetryError, retry, retry_if_result, stop_after_attempt,
                      wait_fixed)

import config
from base.base_crawler import AbstractLogin
from tools import utils, qr_state


class KuaishouLogin(AbstractLogin):
    def __init__(self,
                 login_type: str,
                 browser_context: BrowserContext,
                 context_page: Page,
                 login_phone: Optional[str] = "",
                 cookie_str: str = ""
                 ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    async def begin(self):
        """Start login xiaohongshu"""
        utils.logger.info("[KuaishouLogin.begin] Begin login kuaishou ...")
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "phone":
            await self.login_by_mobile()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError("[KuaishouLogin.begin] Invalid Login Type Currently only supported qrcode or phone or cookie ...")

    @retry(stop=stop_after_attempt(600), wait=wait_fixed(1), retry=retry_if_result(lambda value: value is False))
    async def check_login_state(self) -> bool:
        """
            Check if the current login status is successful and return True otherwise return False
            retry decorator will retry 20 times if the return value is False, and the retry interval is 1 second
            if max retry times reached, raise RetryError
        """
        current_cookie = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(current_cookie)
        kuaishou_pass_token = cookie_dict.get("passToken")
        if kuaishou_pass_token:
            return True
        return False

    async def login_by_qrcode(self):
        """login kuaishou website and keep webdriver login state"""
        utils.logger.info("[KuaishouLogin.login_by_qrcode] Begin login kuaishou by qrcode ...")

        # click login button
        # 2026-11 实测：?isHome=1 首页入口为右上角 p.user-default「登录」，
        # 视频页入口为左侧「立即登录」按钮；静音提示等临时浮层可能拦截点击，
        # 因此双选择器 + 短超时重试，最后兜底 JS 点击（仅用于打开登录弹窗）。
        clicked = False
        for _ in range(3):
            for sel in ("p.user-default", "text=立即登录"):
                loc = self.context_page.locator(sel).first
                if await loc.count() == 0:
                    continue
                try:
                    await loc.click(timeout=5000)
                    clicked = True
                    break
                except Exception:
                    await asyncio.sleep(2)  # 等临时浮层消失再试
            if clicked:
                break
        if not clicked:
            await self.context_page.evaluate(
                "() => { const el = document.querySelector('p.user-default'); if (el) el.click(); }")

        # find login qrcode
        qrcode_img_selector = "//div[@class='qrcode-img']//img"
        base64_qrcode_img = await utils.find_login_qrcode(
            self.context_page,
            selector=qrcode_img_selector
        )
        if not base64_qrcode_img:
            utils.logger.info("[KuaishouLogin.login_by_qrcode] login failed , have not found qrcode please check ....")
            sys.exit()


        # 内嵌展示二维码：写入状态供工作台浮层轮询，不弹任何系统窗口
        qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_WAITING, base64_qrcode_img)
        utils.logger.info("[KuaishouLogin.login_by_qrcode] 请使用「快手 App」扫码，等待中...")
        try:
            await self.check_login_state()
        except RetryError:
            qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_TIMEOUT)
            utils.logger.info("[KuaishouLogin.login_by_qrcode] Login kuaishou failed by qrcode login method ...")
            sys.exit()

        qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_SUCCESS)
        wait_redirect_seconds = 5
        utils.logger.info(f"[KuaishouLogin.login_by_qrcode] Login successful then wait for {wait_redirect_seconds} seconds redirect ...")
        await asyncio.sleep(wait_redirect_seconds)

    async def login_by_mobile(self):
        pass

    async def login_by_cookies(self):
        utils.logger.info("[KuaishouLogin.login_by_cookies] Begin login kuaishou by cookie ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            await self.browser_context.add_cookies([{
                'name': key,
                'value': value,
                'domain': ".kuaishou.com",
                'path': "/"
            }])
