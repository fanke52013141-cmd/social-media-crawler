# -*- coding: utf-8 -*-
# 今日头条扫码登录
import asyncio
from typing import Optional

from playwright.async_api import BrowserContext, Page
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

import config
from base.base_crawler import AbstractLogin
from tools import qr_state, utils

# 登录成功后会写入的会话 cookie（任一存在即视为已登录）
_LOGIN_COOKIE_KEYS = ("sessionid", "sid_tt", "uid_tt", "sessionid_ss")


class ToutiaoLogin(AbstractLogin):
    def __init__(
        self,
        login_type: str,
        browser_context: BrowserContext,
        context_page: Page,
        login_phone: Optional[str] = "",
        cookie_str: str = "",
    ):
        config.LOGIN_TYPE = login_type
        self.browser_context = browser_context
        self.context_page = context_page
        self.login_phone = login_phone
        self.cookie_str = cookie_str

    async def begin(self):
        utils.logger.info("[ToutiaoLogin.begin] Begin login toutiao ...")
        if config.LOGIN_TYPE == "qrcode":
            await self.login_by_qrcode()
        elif config.LOGIN_TYPE == "cookie":
            await self.login_by_cookies()
        else:
            raise ValueError(
                "[ToutiaoLogin.begin] Unsupported login type, currently only qrcode/cookie ..."
            )

    @retry(
        stop=stop_after_attempt(180),
        wait=wait_fixed(1),
        retry=retry_if_result(lambda v: v is False),
    )
    async def check_login_state(self) -> bool:
        cookies = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(cookies)
        for key in _LOGIN_COOKIE_KEYS:
            if cookie_dict.get(key):
                return True
        return False

    async def _has_login_cookie(self) -> bool:
        """即时登录态判断（不带 tenacity 重试），供等待循环调用。"""
        cookies = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(cookies)
        return any(cookie_dict.get(k) for k in _LOGIN_COOKIE_KEYS)

    async def login_by_qrcode(self):
        utils.logger.info("[ToutiaoLogin.login_by_qrcode] Begin toutiao qrcode login ...")
        page = self.context_page

        # 等待页面加载稳定，避免 domcontentloaded 后立即操作导致二维码渲染未就绪
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        # 固定等待页面渲染就绪（与已验证的诊断脚本一致）
        await page.wait_for_timeout(5000)

        # 登录弹窗精确选择器为 .ttp-modal（页面另有天气弹窗也是 role=dialog，需排除）
        # 注意：必须用 Playwright 真实鼠标点击，JS element.click() 无法可靠触发弹窗
        qr_sel = ".web-login-scan-code__content__qrcode-wrapper__qrcode"
        modal = page.locator(".ttp-modal")

        # 若弹窗未可见，则点击可见的登录入口并等待弹窗可见（最多重试 3 轮）
        for attempt in range(3):
            if await modal.count() and await modal.is_visible():
                break
            btns = page.locator("a.login-button")
            for k in range(await btns.count()):
                if await btns.nth(k).is_visible():
                    await btns.nth(k).click(force=True)
                    utils.logger.info(
                        "[ToutiaoLogin] 真实点击 login-button index=%s", k
                    )
                    break
            try:
                await page.wait_for_selector(".ttp-modal", state="visible", timeout=10000)
            except Exception:
                if attempt == 2:
                    raise

        # 等待二维码 img 可见（src 为 data:image/jpeg base64）
        qr_src = None
        for _ in range(15):
            qr = page.locator(qr_sel)
            if await qr.count() and await qr.first.is_visible():
                qr_src = await qr.first.get_attribute("src")
                if qr_src:
                    break
            await asyncio.sleep(1)
        if not qr_src:
            raise RuntimeError("[ToutiaoLogin] qrcode image not found")

        # 写二维码状态：前端内嵌展示，不弹任何系统窗口
        qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_WAITING, qr_src)
        utils.logger.info(
            "[ToutiaoLogin.login_by_qrcode] 请使用「今日头条 App」扫码登录，等待中..."
        )

        # 等待登录：二维码约 60 秒过期，遮罩出现后自动点击刷新并覆盖状态
        mask_sel = ".web-login-scan-code__content__qrcode-wrapper__mask"
        wait_total = 300
        logged = False
        for _ in range(wait_total):
            if await self._has_login_cookie():
                logged = True
                break
            mask = page.locator(mask_sel)
            if await mask.count() and await mask.first.is_visible():
                try:
                    await mask.first.click(force=True)
                    # 等待新二维码元素渲染出现（点击后旧 img 会被短暂替换）
                    await page.wait_for_selector(qr_sel, state="attached", timeout=15000)
                    await page.wait_for_timeout(1500)
                    new_src = await page.locator(qr_sel).first.get_attribute(
                        "src", timeout=15000
                    )
                    if new_src:
                        qr_state.write_qr_state(
                            config.PLATFORM, qr_state.STATUS_WAITING, new_src
                        )
                        utils.logger.info(
                            "[ToutiaoLogin] 二维码已过期，自动刷新"
                        )
                except Exception as exc:
                    utils.logger.warning(
                        "[ToutiaoLogin] 刷新二维码异常，下一轮重试: %s",
                        str(exc)[:60],
                    )
            await asyncio.sleep(1)
        if not logged:
            qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_TIMEOUT)
            utils.logger.error("[ToutiaoLogin.login_by_qrcode] 扫码登录超时，未检测到登录态")
            raise TimeoutError("[ToutiaoLogin] qrcode login timeout")

        # 登录成功
        qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_SUCCESS)
        await asyncio.sleep(3)
        # 登录成功后尝试关闭页面弹窗
        close_btn = await page.query_selector(".ttp-modal-close-btn")
        if close_btn:
            try:
                await close_btn.click()
            except Exception:
                pass
        utils.logger.info("[ToutiaoLogin.login_by_qrcode] Toutiao login success")

    async def login_by_mobile(self):
        # 手机验证码登录需人工收码，暂不实现自动化，保留接口
        raise NotImplementedError("Toutiao mobile login is not automated, please use qrcode")

    async def login_by_cookies(self):
        utils.logger.info("[ToutiaoLogin.login_by_cookies] Begin toutiao cookie login ...")
        for key, value in utils.convert_str_cookie_to_dict(self.cookie_str).items():
            await self.browser_context.add_cookies(
                [
                    {
                        "name": key,
                        "value": value,
                        "domain": ".toutiao.com",
                        "path": "/",
                    }
                ]
            )
