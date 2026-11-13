# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/client.py
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
import json
import random
import re
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union
from urllib.parse import quote, urlencode

import httpx
from playwright.async_api import BrowserContext, Page
from tenacity import (
    RetryError,
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_fixed,
    wait_random_exponential,
)
from tools.tls_client import make_tls_client as make_async_client

import config
from base.base_crawler import AbstractApiClient
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils
from tools.humanize import HumanRhythm
from tools import risk_signals

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import (
    CaptchaError,
    DataFetchError,
    IPBlockError,
    NoteNotFoundError,
    PlatformAccessError,
    RiskControlError,
)
from .field import SearchNoteType, SearchSortType
from .help import get_search_id
from .extractor import XiaoHongShuExtractor
from .playwright_sign import sign_with_xhshow


def _request_stop(retry_state):
    """request 的重试停止条件：默认最多 4 次尝试。

    外层自带重试的调用路径（如 get_note_by_id_from_html，外层 3 次）传
    _single_attempt=True，内层只尝试 1 次，避免内外重试相乘（3×4=12 次传输）。
    """
    if retry_state.kwargs.get("_single_attempt"):
        return True
    return retry_state.attempt_number >= 4


class XiaoHongShuClient(AbstractApiClient, ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Xiaohongshu long videos need longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
        captcha_handler=None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        # 可选的验证码处理协程 (verify_type, verify_uuid) -> None；由 core 注入以自动切有头过码
        self.captcha_handler = captcha_handler
        if config.XHS_INTERNATIONAL:
            self._host = "https://webapi.rednote.com"
            self._domain = "https://www.rednote.com"
        else:
            self._host = "https://edith.xiaohongshu.com"
            self._domain = "https://www.xiaohongshu.com"
        self.cookie_urls = [self._domain]
        self.IP_ERROR_STR = "Network connection error, please check network settings or restart"
        self.IP_ERROR_CODE = 300012
        self.SECURITY_LIMIT_CODE = 300011
        self.NOTE_NOT_FOUND_CODE = -510000
        self.NOTE_ABNORMAL_STR = "Note status abnormal, please check later"
        self.NOTE_ABNORMAL_CODE = -510001
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._extractor = XiaoHongShuExtractor()
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)
        # 公共拟人化节奏（真人动作 + 随机等待 + 长尾停顿 + 会话休息）
        self._rhythm = HumanRhythm(playwright_page)
        # UI 驱动桥接层（config.UI_DRIVEN 时由 core 注入）：
        # 取数走真实页面操作+响应拦截，UI 异常自动回退本文件的 API 路径。
        self.ui_bridge = None

    async def _human_action(self):
        """拟人化真人动作（委托公共模块）。"""
        await self._rhythm.human_action()

    async def _human_pause(self, lo: Optional[float] = None, hi: Optional[float] = None):
        """拟人化等待（委托公共模块）。"""
        await self._rhythm.human_pause(lo, hi)

    async def _random_sleep(self, lo: Optional[float] = None, hi: Optional[float] = None):
        """普通接口请求后的拟人化等待（兼容旧调用名）。"""
        await self._rhythm.random_sleep(lo, hi)

    async def _pre_headers(self, url: str, params: Optional[Dict] = None, payload: Optional[Dict] = None) -> Dict:
        """请求头参数签名 (使用 xhshow 纯算法)

        Args:
            url: 请求 URI path
            params: GET 请求参数
            payload: POST 请求参数

        Returns:
            Dict: 签名后的请求头参数
        """
        if params is not None:
            data = params
            method = "GET"
        elif payload is not None:
            data = payload
            method = "POST"
        else:
            raise ValueError("params or payload is required")

        # 使用 xhshow 纯算法生成签名
        signs = sign_with_xhshow(
            uri=url,
            data=data,
            cookie_str=self.headers.get("Cookie", ""),
            method=method,
        )

        headers = {
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
        }
        self.headers.update(headers)
        return self.headers

    @retry(
        stop=_request_stop,
        wait=wait_random_exponential(multiplier=1.5, max=30),
        retry=retry_if_not_exception_type(
            (NoteNotFoundError, IPBlockError, PlatformAccessError, CaptchaError, RiskControlError)
        ),
        reraise=True,
    )
    async def request(self, method, url, **kwargs) -> Union[str, Any]:
        """
        Wrapper for httpx common request method, processes request response
        Args:
            method: Request method
            url: Request URL
            **kwargs: Other request parameters, such as headers, body, etc.

        Returns:

        """
        # Check if proxy is expired before each request
        await self._refresh_proxy_if_expired()

        # 复制一份再剔除内部控制参数：tenacity 的 retry_state.kwargs 引用原始 dict，
        # 直接在原 dict 上 pop 会让后续尝试读不到 _single_attempt 标志。
        kwargs = dict(kwargs)
        kwargs.pop("_single_attempt", None)
        # return response.text
        return_response = kwargs.pop("return_response", False)
        captcha_attempts = 0
        while True:
            async with make_async_client(proxy=self.proxy) as client:
                response = await client.request(method, url, timeout=self.timeout, **kwargs)

            if response.status_code in {401, 403}:
                raise PlatformAccessError(
                    f"XHS request blocked with HTTP {response.status_code}"
                )

            if response.status_code == 429:
                # 熔断：平台已限流，立即终止本轮，交编排器阶梯冷却。
                # 风控窗口内秒级硬重试会把限制升级为警告/封禁（账号已被警告，尤其要收手）。
                utils.logger.warning("HTTP 429 rate limited, abort this run for cooldown")
                risk_signals.record(config.PLATFORM, risk_signals.KIND_RATE_LIMIT,
                                    "HTTP 429 circuit breaker")
                raise RiskControlError("XHS rate limited (HTTP 429), abort for cooldown")

            if response.status_code == 471 or response.status_code == 461:
                verify_type = response.headers["Verifytype"]
                verify_uuid = response.headers["Verifyuuid"]
                utils.logger.warning(
                    f"CAPTCHA appeared (HTTP {response.status_code}, Verifytype {verify_type})"
                )
                risk_signals.record(config.PLATFORM, risk_signals.KIND_CAPTCHA,
                                    f"HTTP {response.status_code} Verifytype {verify_type}")
                # 有 handler 且未超内部重试次数：切有头让用户手动过码，然后重试本次请求
                if self.captcha_handler is not None and captcha_attempts < 2:
                    captcha_attempts += 1
                    await self.captcha_handler(verify_type, verify_uuid)
                    continue
                # 无 handler 或过码后仍被拦：明确失败，不做无意义的秒级硬重试
                raise CaptchaError(
                    f"CAPTCHA required, Verifytype: {verify_type}, Verifyuuid: {verify_uuid}"
                )

            response_data: Optional[Dict] = None
            try:
                candidate_data = response.json()
                if isinstance(candidate_data, dict):
                    response_data = candidate_data
            except (TypeError, ValueError):
                pass

            response_code = (
                str(response_data.get("code"))
                if response_data is not None and response_data.get("code") is not None
                else ""
            )
            if response_code == str(self.IP_ERROR_CODE):
                raise IPBlockError(self.IP_ERROR_STR)
            if response_code == str(self.SECURITY_LIMIT_CODE):
                # 熔断：账号安全限制是警告前兆，立即终止本轮并阶梯冷却，不再退避重试。
                utils.logger.warning("account security limit (300011), abort this run for cooldown")
                risk_signals.record(config.PLATFORM, risk_signals.KIND_SECURITY,
                                    f"code {self.SECURITY_LIMIT_CODE} circuit breaker")
                raise RiskControlError(
                    f"XHS account security restriction, code: {self.SECURITY_LIMIT_CODE}, abort for cooldown"
                )

            if return_response:
                return response.text
            data: Dict = response_data if response_data is not None else response.json()
            if data["success"]:
                return data.get("data", data.get("success", {}))
            # IP_ERROR_CODE / SECURITY_LIMIT_CODE are already handled above, before return_response.
            elif data["code"] in (self.NOTE_NOT_FOUND_CODE, self.NOTE_ABNORMAL_CODE):
                raise NoteNotFoundError(f"Note not found or abnormal, code: {data['code']}")
            else:
                err_msg = data.get("msg", None) or f"{response.text}"
                raise DataFetchError(err_msg)

    @staticmethod
    def _build_query_string(params: Dict) -> str:
        """Build URL query string with encoding matching browser behavior (commas not encoded)"""
        parts = []
        for key, value in params.items():
            value_str = str(value) if value is not None else ""
            parts.append(f"{key}={quote(value_str, safe=',')}")
        return "&".join(parts)

    async def get(self, uri: str, params: Optional[Dict] = None) -> Dict:
        """
        GET request, signs request headers
        Args:
            uri: Request route
            params: Request parameters

        Returns:

        """
        headers = await self._pre_headers(uri, params)
        # Build URL manually to ensure query string encoding matches the sign string
        # (httpx's default params encoding differs from browser/XHS frontend behavior)
        if params:
            full_url = f"{self._host}{uri}?{self._build_query_string(params)}"
        else:
            full_url = f"{self._host}{uri}"

        return await self.request(
            method="GET", url=full_url, headers=headers
        )

    async def post(self, uri: str, data: dict, **kwargs) -> Dict:
        """
        POST request, signs request headers
        Args:
            uri: Request route
            data: Request body parameters

        Returns:

        """
        headers = await self._pre_headers(uri, payload=data)
        json_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return await self.request(
            method="POST",
            url=f"{self._host}{uri}",
            data=json_str,
            headers=headers,
            **kwargs,
        )

    async def query_self(self) -> Optional[Dict]:
        """
        Query self user info to check login state
        Returns:
            Dict: User info if logged in, None otherwise
        """
        uri = "/api/sns/web/v1/user/selfinfo"
        headers = await self._pre_headers(uri, params={})
        async with make_async_client(proxy=self.proxy) as client:
            response = await client.get(f"{self._host}{uri}", headers=headers)
            if response.status_code == 200:
                return response.json()
        return None

    async def pong(self) -> bool:
        """
        Check if login state is still valid by querying self user info
        Returns:
            bool: True if logged in, False otherwise
        """
        utils.logger.info("[XiaoHongShuClient.pong] Begin to check login state...")
        ping_flag = False
        try:
            self_info: Dict = await self.query_self()
            if self_info and self_info.get("data", {}).get("result", {}).get("success"):
                ping_flag = True
        except Exception as e:
            utils.logger.error(
                f"[XiaoHongShuClient.pong] Check login state failed: {e}, and try to login again..."
            )
            ping_flag = False
        utils.logger.info(f"[XiaoHongShuClient.pong] Login state result: {ping_flag}")
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[list[str]] = None):
        """
        Update cookies method provided by API client, usually called after successful login
        Args:
            browser_context: Browser context object

        Returns:

        """
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser_context,
            urls=urls or self.cookie_urls,
        )
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict

    async def get_note_by_keyword(
        self,
        keyword: str,
        search_id: str = get_search_id(),
        page: int = 1,
        page_size: int = 20,
        sort: SearchSortType = SearchSortType.GENERAL,
        note_type: SearchNoteType = SearchNoteType.ALL,
        time_filter: str = "",
    ) -> Dict:
        """
        Search notes by keyword
        Args:
            keyword: Keyword parameter
            page: Page number
            page_size: Page data length
            sort: Search result sorting specification
            note_type: Type of note to search
            time_filter: UI 面板「发布时间」预筛选项（一天内/一周内/半年内），空为不限

        Returns:

        """
        if self.ui_bridge is not None:
            try:
                return await self.ui_bridge.search_notes(keyword, page, sort, time_filter)
            except (RiskControlError, CaptchaError, IPBlockError,
                    PlatformAccessError, NoteNotFoundError):
                raise
            except Exception as e:
                utils.logger.warning(
                    f"[XiaoHongShuClient] UI 搜索失败，回退 API 路径: {e}")
        uri = "/api/sns/web/v1/search/notes"
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": search_id,
            "sort": sort.value,
            "note_type": note_type.value,
        }
        return await self.post(uri, data)

    async def get_note_by_id(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
    ) -> Dict:
        """
        Get note detail API
        Args:
            note_id: Note ID
            xsec_source: Channel source
            xsec_token: Token returned from search keyword result list

        Returns:

        """
        if self.ui_bridge is not None:
            try:
                return await self.ui_bridge.note_detail(note_id, xsec_source, xsec_token)
            except (RiskControlError, CaptchaError, IPBlockError,
                    PlatformAccessError, NoteNotFoundError):
                raise
            except Exception as e:
                utils.logger.warning(
                    f"[XiaoHongShuClient] UI 详情失败，回退 API 路径 note={note_id}: {e}")
        if xsec_source == "":
            xsec_source = "pc_search"

        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source,
            "xsec_token": xsec_token,
        }
        uri = "/api/sns/web/v1/feed"
        res = await self.post(uri, data)
        if res and res.get("items"):
            res_dict: Dict = res["items"][0]["note_card"]
            return res_dict
        # When crawling frequently, some notes may have results while others don't
        utils.logger.error(
            f"[XiaoHongShuClient.get_note_by_id] get note id:{note_id} empty and res:{res}"
        )
        return dict()

    async def get_note_comments(
        self,
        note_id: str,
        xsec_token: str,
        cursor: str = "",
    ) -> Dict:
        """
        Get first-level comments API
        Args:
            note_id: Note ID
            xsec_token: Verification token
            cursor: Pagination cursor

        Returns:

        """
        uri = "/api/sns/web/v2/comment/page"
        params = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
        }
        return await self.get(uri, params)

    async def get_note_sub_comments(
        self,
        note_id: str,
        root_comment_id: str,
        xsec_token: str,
        num: int = 10,
        cursor: str = "",
    ):
        """
        Get sub-comments under specified parent comment API
        Args:
            note_id: Post ID of sub-comments
            root_comment_id: Root comment ID
            xsec_token: Verification token
            num: Pagination quantity
            cursor: Pagination cursor

        Returns:

        """
        uri = "/api/sns/web/v2/comment/sub/page"
        params = {
            "note_id": note_id,
            "root_comment_id": root_comment_id,
            "num": str(num),
            "cursor": cursor,
            "image_formats": "jpg,webp,avif",
            "top_comment_id": "",
            "xsec_token": xsec_token,
        }
        return await self.get(uri, params)

    async def get_note_all_comments(
        self,
        note_id: str,
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ) -> List[Dict]:
        """
        Get all first-level comments under specified note, this method will continuously find all comment information under a post
        Args:
            note_id: Note ID
            xsec_token: Verification token
            crawl_interval: Crawl delay per note (seconds)
            callback: Callback after one note crawl ends
            max_count: Maximum number of comments to crawl per note
        Returns:

        """
        if self.ui_bridge is not None:
            try:
                return await self._ui_all_comments(note_id, xsec_token, callback, max_count)
            except (RiskControlError, CaptchaError, IPBlockError,
                    PlatformAccessError, NoteNotFoundError):
                raise
            except Exception as e:
                utils.logger.warning(
                    f"[XiaoHongShuClient] UI 评论失败，回退 API 路径 note={note_id}: {e}")
        result = []
        comments_has_more = True
        comments_cursor = ""
        while comments_has_more and len(result) < max_count:
            comments_res = await self.get_note_comments(
                note_id=note_id, xsec_token=xsec_token, cursor=comments_cursor
            )
            comments_has_more = comments_res.get("has_more", False)
            comments_cursor = comments_res.get("cursor", "")
            if "comments" not in comments_res:
                utils.logger.info(
                    f"[XiaoHongShuClient.get_note_all_comments] No 'comments' key found in response: {comments_res}"
                )
                break
            comments = comments_res["comments"]
            if len(result) + len(comments) > max_count:
                comments = comments[: max_count - len(result)]
            if callback:
                await callback(note_id, comments)
            await self._random_sleep()
            result.extend(comments)
            sub_comments = await self.get_comments_all_sub_comments(
                comments=comments,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=callback,
            )
            result.extend(sub_comments)
        return result

    async def _ui_all_comments(
        self,
        note_id: str,
        xsec_token: str,
        callback: Optional[Callable],
        max_count: int,
    ) -> List[Dict]:
        """UI 驱动评论采集：真实滚动评论区/点击展开回复，拦截页面自身的评论响应。
        回调、子评论、数量裁剪逻辑与 API 路径完全一致，存储字段零变化。"""
        result: List[Dict] = []
        async for kind, payload in self.ui_bridge.comment_batches(note_id, xsec_token):
            comments = payload.get("comments") or []
            if not comments:
                if kind == "root" and not payload.get("has_more", False):
                    break
                continue
            if kind == "root":
                if len(result) + len(comments) > max_count:
                    comments = comments[: max_count - len(result)]
                if callback:
                    await callback(note_id, comments)
                await self._random_sleep()
                result.extend(comments)
                # 一级评论内联携带的子评论（与 API 路径同逻辑）
                if config.ENABLE_GET_SUB_COMMENTS:
                    for c in comments:
                        subs = c.get("sub_comments") or []
                        if subs and callback:
                            await callback(c.get("note_id") or note_id, subs)
                        result.extend(subs)
            else:
                if callback:
                    await callback(note_id, comments)
                await self._random_sleep()
                result.extend(comments)
            if len(result) >= max_count:
                break
        return result

    async def get_comments_all_sub_comments(
        self,
        comments: List[Dict],
        xsec_token: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        Get all second-level comments under specified first-level comments, this method will continuously find all second-level comment information under first-level comments
        Args:
            comments: Comment list
            xsec_token: Verification token
            crawl_interval: Crawl delay per comment (seconds)
            callback: Callback after one comment crawl ends

        Returns:

        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            utils.logger.info(
                f"[XiaoHongShuCrawler.get_comments_all_sub_comments] Crawling sub_comment mode is not enabled"
            )
            return []

        result = []
        for comment in comments:
            try:
                note_id = comment.get("note_id")
                sub_comments = comment.get("sub_comments")
                if sub_comments and callback:
                    await callback(note_id, sub_comments)

                sub_comment_has_more = comment.get("sub_comment_has_more")
                if not sub_comment_has_more:
                    continue

                root_comment_id = comment.get("id")
                sub_comment_cursor = comment.get("sub_comment_cursor")

                while sub_comment_has_more:
                    try:
                        comments_res = await self.get_note_sub_comments(
                            note_id=note_id,
                            root_comment_id=root_comment_id,
                            xsec_token=xsec_token,
                            num=10,
                            cursor=sub_comment_cursor,
                        )

                        if comments_res is None:
                            utils.logger.info(
                                f"[XiaoHongShuClient.get_comments_all_sub_comments] No response found for note_id: {note_id}"
                            )
                            break
                        sub_comment_has_more = comments_res.get("has_more", False)
                        sub_comment_cursor = comments_res.get("cursor", "")
                        if "comments" not in comments_res:
                            utils.logger.info(
                                f"[XiaoHongShuClient.get_comments_all_sub_comments] No 'comments' key found in response: {comments_res}"
                            )
                            break
                        comments = comments_res["comments"]
                        if callback:
                            await callback(note_id, comments)
                        await self._random_sleep()
                        result.extend(comments)
                    except DataFetchError as e:
                        utils.logger.warning(
                            f"[XiaoHongShuClient.get_comments_all_sub_comments] Failed to get sub-comments for note_id: {note_id}, root_comment_id: {root_comment_id}, error: {e}. Skipping this comment's sub-comments."
                        )
                        break  # Break out of the sub-comment acquisition loop of the current comment and continue processing the next comment
                    except Exception as e:
                        utils.logger.error(
                            f"[XiaoHongShuClient.get_comments_all_sub_comments] Unexpected error when getting sub-comments for note_id: {note_id}, root_comment_id: {root_comment_id}, error: {e}"
                        )
                        break
            except Exception as e:
                utils.logger.error(
                    f"[XiaoHongShuClient.get_comments_all_sub_comments] Error processing comment: {comment.get('id', 'unknown')}, error: {e}. Continuing with next comment."
                )
                continue  # Continue to next comment
        return result

    async def get_creator_info(
        self, user_id: str, xsec_token: str = "", xsec_source: str = ""
    ) -> Dict:
        """通过浏览器页面访问用户主页，解析小红书号等信息。
        用独立 page 访问（带完整登录态、可执行 JS），避免 httpx 被重定向到登录页。
        返回扁平结构 {red_id, nickname, ip_location}。"""
        uri = f"/user/profile/{user_id}"
        if xsec_token and xsec_source:
            uri = f"{uri}?xsec_token={xsec_token}&xsec_source={xsec_source}"
        target = self._domain + uri

        page = await self.playwright_page.context.new_page()
        try:
            await page.goto(target, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            # 真实访问主页：拟人化浏览停留后再解析，而非落地即取数
            rhythm = HumanRhythm(page)
            await rhythm.browse_step()
            await asyncio.sleep(random.uniform(3, 8))

            info = await page.evaluate(
                """() => {
                  const st = window.__INITIAL_STATE__;
                  let d = st && st.user && st.user.userPageData;
                  if (!d) return null;
                  if (d.__v_isRef || d.__v_isShallow) d = d._value || d._rawValue;
                  if (!d) return null;
                  const b = d.basicInfo || d.basic_info || {};
                  return {
                    red_id: b.redId || b.red_id || '',
                    nickname: b.nickname || '',
                    ip_location: b.ipLocation || b.ip_location || ''
                  };
                }""",
                isolated_context=False,
            )
            if info and info.get("red_id"):
                return info

            # 兜底：从 DOM 文本提取“小红书号：xxx”
            body = await page.locator("body").inner_text()
            m = re.search(r"小红书号[：:]\s*([A-Za-z0-9_\-]+)", body)
            base = info or {}
            return {
                "red_id": m.group(1) if m else "",
                "nickname": base.get("nickname", ""),
                "ip_location": base.get("ip_location", ""),
            }
        finally:
            await page.close()

    async def get_notes_by_creator(
        self,
        creator: str,
        cursor: str,
        page_size: int = 30,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> Dict:
        """
        Get creator's notes
        Args:
            creator: Creator ID
            cursor: Last note ID from previous page
            page_size: Page data length
            xsec_token: Verification token
            xsec_source: Channel source

        Returns:

        """
        uri = f"/api/sns/web/v1/user_posted"
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": creator,
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
            "xsec_source": xsec_source,
        }
        return await self.get(uri, params)

    async def get_all_notes_by_creator(
        self,
        user_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> List[Dict]:
        """
        Get all posts published by specified user, this method will continuously find all post information under a user
        Args:
            user_id: User ID
            crawl_interval: Crawl delay (seconds)
            callback: Update callback function after one pagination crawl ends
            xsec_token: Verification token
            xsec_source: Channel source

        Returns:

        """
        result = []
        notes_has_more = True
        notes_cursor = ""
        while notes_has_more and len(result) < config.CRAWLER_MAX_NOTES_COUNT:
            notes_res = await self.get_notes_by_creator(
                user_id, notes_cursor, xsec_token=xsec_token, xsec_source=xsec_source
            )
            if not notes_res:
                utils.logger.error(
                    f"[XiaoHongShuClient.get_notes_by_creator] The current creator may have been banned by xhs, so they cannot access the data."
                )
                break

            notes_has_more = notes_res.get("has_more", False)
            notes_cursor = notes_res.get("cursor", "")
            if "notes" not in notes_res:
                utils.logger.info(
                    f"[XiaoHongShuClient.get_all_notes_by_creator] No 'notes' key found in response: {notes_res}"
                )
                break

            notes = notes_res["notes"]
            utils.logger.info(
                f"[XiaoHongShuClient.get_all_notes_by_creator] got user_id:{user_id} notes len : {len(notes)}"
            )

            remaining = config.CRAWLER_MAX_NOTES_COUNT - len(result)
            if remaining <= 0:
                break

            notes_to_add = notes[:remaining]
            if callback:
                await callback(notes_to_add)

            result.extend(notes_to_add)
            await self._random_sleep()

        utils.logger.info(
            f"[XiaoHongShuClient.get_all_notes_by_creator] Finished getting notes for user {user_id}, total: {len(result)}"
        )
        return result

    async def get_note_short_url(self, note_id: str) -> Dict:
        """
        Get note short URL
        Args:
            note_id: Note ID

        Returns:

        """
        uri = f"/api/sns/web/short_url"
        data = {"original_url": f"{self._domain}/discovery/item/{note_id}"}
        return await self.post(uri, data=data, return_response=True)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_not_exception_type(
            (RetryError, IPBlockError, PlatformAccessError)
        ),
    )
    async def get_note_by_id_from_html(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        enable_cookie: bool = False,
    ) -> Optional[Dict]:
        """
        Get note details by parsing note detail page HTML, this interface may fail, retry 3 times here
        copy from https://github.com/ReaJason/xhs/blob/eb1c5a0213f6fbb592f0a2897ee552847c69ea2d/xhs/core.py#L217-L259
        thanks for ReaJason
        Args:
            note_id:
            xsec_source:
            xsec_token:
            enable_cookie:

        Returns:

        """
        url = (
            f"{self._domain}/explore/"
            + note_id
            + f"?xsec_token={xsec_token}&xsec_source={xsec_source}"
        )
        copy_headers = self.headers.copy()
        if not enable_cookie:
            del copy_headers["Cookie"]

        html = await self.request(
            method="GET", url=url, return_response=True, headers=copy_headers,
            _single_attempt=True,
        )

        return self._extractor.extract_note_detail_from_html(note_id, html)
