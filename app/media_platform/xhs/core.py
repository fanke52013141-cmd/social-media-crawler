# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
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
import os
import random
import time
from asyncio import Task
from typing import Dict, List, Optional

from patchright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    async_playwright,
)
from tenacity import RetryError

import config
from base.base_crawler import AbstractCrawler
from media_downloader import MediaDownloader
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import utils, browser_launch, time_util
from tools import profile_fp
from tools.cdp_browser import CDPBrowserManager
from tools.login_state import mark_logged_in
from var import crawler_type_var, source_keyword_var

from . import media as xhs_media
from .client import XiaoHongShuClient
from .exception import (
    CaptchaError,
    DataFetchError,
    IPBlockError,
    NoteNotFoundError,
    PlatformAccessError,
)
from .field import SearchSortType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin


class XiaoHongShuCrawler(AbstractCrawler):
    context_page: Page
    xhs_client: XiaoHongShuClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self) -> None:
        self.index_url = "https://www.rednote.com" if config.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
        self.cookie_urls = [self.index_url]
        # self.user_agent = utils.get_user_agent()
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self._media_downloader: Optional[MediaDownloader] = None
        # user_id -> 小红书号(red_id)，全局去重；启动时从本地文件加载，跨任务复用
        self._red_id_cache: Dict[str, str] = self._load_red_id_cache()
        self._new_creator_visited = 0  # 本次运行内新访问主页计数，用于插入长休息（无人数上限）
        self._playwright = None  # 保存 playwright 实例，供验证码处理时重新启动有头浏览器
        self._busy_solving_captcha = False  # 防止验证码处理重入

    def _load_red_id_cache(self) -> Dict[str, str]:
        """从本地文件加载小红书号缓存，避免重复访问已解析过的主页。"""
        try:
            p = config.RED_ID_CACHE_FILE
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items() if v}
        except Exception as e:
            utils.logger.warning(f"[XiaoHongShuCrawler._load_red_id_cache] 加载缓存失败: {e}")
        return {}

    def _save_red_id_cache(self) -> None:
        """把小红书号缓存写入本地文件，供后续任务复用。"""
        try:
            p = config.RED_ID_CACHE_FILE
            d = os.path.dirname(p)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self._red_id_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            utils.logger.warning(f"[XiaoHongShuCrawler._save_red_id_cache] 写入缓存失败: {e}")

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            self._playwright = playwright
            # Choose launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.HEADLESS,
                )
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            # Create a client to interact with the Xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if config.CRAWL_MODE == "guest":
                # 游客模式：不检查登录态、不扫码，直接以游客身份采集公开可见内容
                utils.logger.info(
                    "[XiaoHongShuCrawler.start] Guest mode enabled, skip login and crawl public content only"
                )
            elif not await self.xhs_client.pong():
                login_obj = XiaoHongShuLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # input your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()
                mark_logged_in(config.PLATFORM)
                await self.xhs_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )
            else:
                # 已登录、无需扫码：校正可能过期的缓存
                mark_logged_in(config.PLATFORM)

            if getattr(config, "LOGIN_ONLY", False):
                utils.logger.info("[Crawler.start] login-only mode, finished after login, skip crawling")
                return
            crawler_type_var.set(config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for notes and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            else:
                pass

            utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        xhs_limit_count = 20  # Xiaohongshu limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
        start_page = config.START_PAGE
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            page = 1
            search_id = get_search_id()
            while (page - start_page + 1) * xhs_limit_count <= config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search Xiaohongshu keyword: {keyword}, page: {page}")
                    note_ids: List[str] = []
                    xsec_tokens: List[str] = []
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        search_id=search_id,
                        page=page,
                        sort=(SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != "" else SearchSortType.GENERAL),
                    )
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Search notes response: {notes_res}")
                    if not notes_res or not notes_res.get("has_more", False):
                        utils.logger.info("[XiaoHongShuCrawler.search] No more content!")
                        break
                    semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
                    task_list = [
                        self.get_note_detail_async_task(
                            note_id=post_item.get("id"),
                            xsec_source=post_item.get("xsec_source"),
                            xsec_token=post_item.get("xsec_token"),
                            semaphore=semaphore,
                        ) for post_item in notes_res.get("items", {}) if post_item.get("model_type") not in ("rec_query", "hot_query")
                    ]
                    note_details = await asyncio.gather(*task_list)
                    for note_detail in note_details:
                        if note_detail:
                            # 时间窗过滤：发布时间不在窗口内的跳过，不入库、不计数
                            if not time_util.in_window(note_detail.get("time")):
                                utils.logger.info(
                                    f"[XiaoHongShuCrawler.search] skip out-of-window note {note_detail.get('note_id')}, "
                                    f"time={note_detail.get('time')}"
                                )
                                continue
                            await xhs_store.update_xhs_note(note_detail)
                            await self.download_media(note_detail)
                            note_ids.append(note_detail.get("note_id"))
                            xsec_tokens.append(note_detail.get("xsec_token"))
                    page += 1
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Note details: {note_details}")
                    await self.batch_get_note_comments(note_ids, xsec_tokens)

                    # 拟人化等待（真人鼠标动作 + 长尾停顿 + 会话休息）
                    await self.xhs_client._random_sleep()
                    utils.logger.info(f"[XiaoHongShuCrawler.search] 页面 {page-1} 后拟人化等待完成")
                except DataFetchError as ex:
                    utils.logger.error(f"[XiaoHongShuCrawler.search] Get note detail error: {ex}")
                    break

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get Xiaohongshu creators")
        for creator_url in config.XHS_CREATOR_ID_LIST:
            try:
                # Parse creator URL to get user_id and security tokens
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[XiaoHongShuCrawler.get_creators_and_notes] Parse creator URL info: {creator_info}")
                user_id = creator_info.user_id

                # get creator detail info from web html content
                createor_info: Dict = await self.xhs_client.get_creator_info(
                    user_id=user_id,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source
                )
                if createor_info:
                    await xhs_store.save_creator(user_id, creator=createor_info)
            except ValueError as e:
                utils.logger.error(f"[XiaoHongShuCrawler.get_creators_and_notes] Failed to parse creator URL: {e}")
                continue
            except (IPBlockError, PlatformAccessError) as e:
                # Access restricted on the creator homepage, skip this creator instead of crashing the run.
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_creators_and_notes] Access restricted for creator {creator_url}: {e}. "
                    f"建议降低采集频率、更换 IP 或检查账号状态"
                )
                continue

            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            # Get all note information of the creator
            all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                user_id=user_id,
                crawl_interval=crawl_interval,
                callback=self.fetch_creator_notes_detail,
                xsec_token=creator_info.xsec_token,
                xsec_source=creator_info.xsec_source,
            )

            note_ids = []
            xsec_tokens = []
            for note_item in all_notes_list:
                note_ids.append(note_item.get("note_id"))
                xsec_tokens.append(note_item.get("xsec_token"))
            await self.batch_get_note_comments(note_ids, xsec_tokens)

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """Concurrently obtain the specified post list and save the data"""
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [
            self.get_note_detail_async_task(
                note_id=post_item.get("note_id"),
                xsec_source=post_item.get("xsec_source"),
                xsec_token=post_item.get("xsec_token"),
                semaphore=semaphore,
            ) for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail:
                await xhs_store.update_xhs_note(note_detail)
                await self.download_media(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        get_note_detail_task_list = []
        for full_note_url in config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            utils.logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
            crawler_task = self.get_note_detail_async_task(
                note_id=note_url_info.note_id,
                xsec_source=note_url_info.xsec_source,
                xsec_token=note_url_info.xsec_token,
                semaphore=asyncio.Semaphore(config.MAX_CONCURRENCY_NUM),
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await xhs_store.update_xhs_note(note_detail)
                await self.download_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

    async def get_note_detail_async_task(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """Get note detail

        Args:
            note_id:
            xsec_source:
            xsec_token:
            semaphore:

        Returns:
            Dict: note detail
        """
        note_detail = None
        utils.logger.info(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
        async with semaphore:
            try:
                try:
                    note_detail = await self.xhs_client.get_note_by_id(note_id, xsec_source, xsec_token)
                except RetryError:
                    pass

                if not note_detail:
                    note_detail = await self.xhs_client.get_note_by_id_from_html(note_id, xsec_source, xsec_token,
                                                                                 enable_cookie=True)
                    if not note_detail:
                        utils.logger.warning(f"[skip] Failed to get note detail, Id: {note_id}, 跳过继续")
                        return None

                note_detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})

                # 拟人化等待（真人鼠标动作 + 长尾停顿 + 会话休息）
                await self.xhs_client._random_sleep()
                utils.logger.info(f"[get_note_detail_async_task] 笔记 {note_id} 后拟人化等待完成")

                return note_detail

            except NoteNotFoundError as ex:
                utils.logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: {note_id}, {ex}")
                return None
            except (IPBlockError, PlatformAccessError) as ex:
                # Access restricted (IP block / rate limit / account security).
                # Skip this note instead of aborting the whole asyncio.gather batch.
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_note_detail_async_task] Access restricted while getting note {note_id}: {ex}. "
                    f"建议降低采集频率、更换 IP 或检查账号状态"
                )
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_note_comments(self, note_list: List[str], xsec_tokens: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for index, note_id in enumerate(note_list):
            task = asyncio.create_task(
                self.get_comments(note_id=note_id, xsec_token=xsec_tokens[index], semaphore=semaphore),
                name=note_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list)

    @staticmethod
    def _pick_red_id(info):
        """从 get_creator_info 返回的扁平结构中提取小红书号。"""
        if not info:
            return ""
        return info.get("red_id") or ""

    async def _enrich_and_store(self, note_id: str, comments: List[Dict]):
        """评论入库前解析每个评论人的主页，补充小红书号(red_id)，再走原存储。
        全局按 user_id 去重、不限制人数；一级评论与子评论都经过这里。
        开关 config.ENABLE_GET_RED_ID：关闭后不发起任何主页访问（最敏感请求），
        仅复用本地已缓存的号，评论照常采集、更安全更快。
        安全保守模式：新主页访问慢速拟人化 + 周期性长休息，缓存跨任务持久化复用。"""
        parse_red = config.ENABLE_GET_RED_ID
        for c in comments:
            ui = c.get("user_info") or {}
            uid = ui.get("user_id")
            if uid:
                if parse_red and (uid not in self._red_id_cache or not self._red_id_cache.get(uid)):
                    self._red_id_cache[uid] = ""  # 占位，避免并发重复请求
                    rid = ""
                    try:
                        info = await self.xhs_client.get_creator_info(uid)
                        rid = self._pick_red_id(info)
                    except Exception as e:
                        utils.logger.warning(
                            f"[XiaoHongShuCrawler._enrich_and_store] get creator red_id failed uid={uid}: {e}"
                        )
                    self._red_id_cache[uid] = rid or ""
                    self._save_red_id_cache()  # 实时持久化，中断也不丢已解析结果
                    self._new_creator_visited += 1
                    # 拟人化慢速等待：访问主页后做真人动作并随机等待，节奏明显放慢
                    await self.xhs_client._human_action()
                    await asyncio.sleep(random.uniform(config.CREATOR_SLEEP_MIN, config.CREATOR_SLEEP_MAX))
                    # 每访问若干个新主页插一次长休息，歇完继续（无人数上限）
                    if self._new_creator_visited % config.CREATOR_REST_EVERY == 0:
                        rest = random.uniform(config.CREATOR_LONG_REST_MIN, config.CREATOR_LONG_REST_MAX)
                        utils.logger.info(
                            f"[XiaoHongShuCrawler._enrich_and_store] 已访问 {self._new_creator_visited} 个主页，长休息 {rest:.0f}s 后继续"
                        )
                        await asyncio.sleep(rest)
                ui["red_id"] = self._red_id_cache.get(uid) or ""
            else:
                ui["red_id"] = ""
        await xhs_store.batch_update_xhs_note_comments(note_id, comments)

    async def get_comments(self, note_id: str, xsec_token: str, semaphore: asyncio.Semaphore):
        """Get note comments with keyword filtering and quantity limitation"""
        async with semaphore:
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            # Use fixed crawling interval
            crawl_interval = config.CRAWLER_MAX_SLEEP_SEC
            await self.xhs_client.get_note_all_comments(
                note_id=note_id,
                xsec_token=xsec_token,
                crawl_interval=crawl_interval,
                callback=self._enrich_and_store,
                max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
            )

            # 帖子评论拉完后拟人化等待，再进入下一条
            await self.xhs_client._random_sleep()
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] 帖子 {note_id} 评论后拟人化等待完成")

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self.browser_context,
            urls=self.cookie_urls,
        )
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.index_url,
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": f"{self.index_url}/",
                "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
            captcha_handler=self._handle_captcha,  # 撞验证码时自动切有头，手动过码后继续
        )
        return xhs_client_obj

    async def _is_verified_on_page(self, before_session: Optional[str]) -> bool:
        """通过浏览器页面判断验证是否已通过（不经过 python client，避免验证码处理重入）。"""
        # 信号1：侧栏出现“我”入口，说明已登录/已放行
        try:
            me_selector = "xpath=//a[contains(@href, '/user/profile/')]//span[text()='我']"
            if await self.context_page.is_visible(me_selector, timeout=500):
                return True
        except Exception:
            pass
        # 信号2：web_session 已刷新（与登录模块的 cookie 判断一致）
        try:
            _, now_dict = utils.convert_cookies(await self.browser_context.cookies())
            now_session = now_dict.get("web_session")
            if now_session and now_session != before_session:
                return True
        except Exception:
            pass
        return False

    async def _handle_captcha(self, verify_type, verify_uuid):
        """检测到人机验证(HTTP 461/471)：关闭无头 context、有头重启，等待用户手动过码，再同步新 cookie。"""
        if self._busy_solving_captcha:
            # 重入保护：若已有处理流程在进行，稍等后返回交由外层重试
            await asyncio.sleep(2)
            return
        self._busy_solving_captcha = True
        try:
            utils.logger.warning("检测到人机验证，正在切换到有头模式，请在弹出的浏览器中完成验证 ...")
            # 1. 关闭当前无头 context
            try:
                await self.browser_context.close()
            except Exception:
                pass
            # 2. 有头模式重新启动持久 context（同一 user-data-dir，登录态保留）
            chromium = self._playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium, None, self.user_agent, headless=False
            )
            await self.browser_context.add_init_script(path="libs/stealth.min.js")
            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)

            _, before_dict = utils.convert_cookies(await self.browser_context.cookies())
            before_session = before_dict.get("web_session")

            # 3. 轮询等待用户完成验证（最多 150 秒）
            deadline = time.time() + 150
            solved = False
            while time.time() < deadline:
                await asyncio.sleep(2)
                if await self._is_verified_on_page(before_session):
                    solved = True
                    break
            if not solved:
                raise CaptchaError("等待人工验证超时（150 秒内未完成）")

            # 4. 同步最新 cookie 到当前 client（client 对象不变，仅更新 cookie），随后 request 会重试
            cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
                self.browser_context, urls=self.cookie_urls
            )
            self.xhs_client.headers["Cookie"] = cookie_str
            self.xhs_client.cookie_dict = cookie_dict
            utils.logger.info("人机验证已通过，继续采集 ...")
        finally:
            self._busy_solving_captcha = False

    # 常见桌面分辨率：每次启动随机选择，避免固定视窗指纹，均为真实常见尺寸
    COMMON_VIEWPORTS = [
        {"width": 1920, "height": 1080},
        {"width": 1536, "height": 864},
        {"width": 1366, "height": 768},
        {"width": 1440, "height": 900},
        {"width": 1600, "height": 900},
    ]

    @classmethod
    def _random_viewport(cls):
        # 稳定指纹：首次分配后长期固定，而非每次启动都随机
        return profile_fp.get_viewport(config.PLATFORM)

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        viewport = self._random_viewport()
        if config.CRAWL_MODE == "guest":
            # 游客模式：使用独立的游客浏览器目录，绝不加载/污染登录账号目录，也不保存任何账号登录态
            guest_dir = os.path.join(
                os.getcwd(), "browser_data", f"{config.PLATFORM}_guest_user_data_dir"
            )
            utils.logger.info("[XiaoHongShuCrawler.launch_browser] Guest mode, using isolated guest profile")
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=guest_dir,
                accept_downloads=True,
                headless=browser_launch.effective_headless(config.PLATFORM, headless),
                proxy=playwright_proxy,  # type: ignore
                viewport=viewport,
                user_agent=user_agent,
                channel=browser_launch.browser_channel(config.PLATFORM),
                args=browser_launch.offscreen_args(config.PLATFORM),
            )
            return browser_context
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=browser_launch.effective_headless(config.PLATFORM, headless),
                proxy=playwright_proxy,  # type: ignore
                viewport=viewport,
                user_agent=user_agent,
                channel=browser_launch.browser_channel(config.PLATFORM),
                args=browser_launch.offscreen_args(config.PLATFORM),
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy)  # type: ignore
            browser_context = await browser.new_context(viewport=viewport, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser using CDP mode"""
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[XiaoHongShuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def download_media(self, note_detail: Dict) -> None:
        """下载笔记的媒体资源（封面、视频或图文图片）

        Args:
            note_detail: 笔记详情（原始 note_card 结构）
        """
        if not config.ENABLE_GET_MEDIA:
            return
        try:
            items = xhs_media.build_media_items(note_detail)
            if items:
                await self._get_media_downloader().download_all(items)
        except Exception as exc:
            # 媒体下载是旁路能力，解析异常/网络异常都不能中断爬取主流程
            utils.logger.error(f"[XiaoHongShuCrawler.download_media] 媒体下载异常: {exc}")

    def _get_media_downloader(self) -> MediaDownloader:
        """惰性创建媒体下载器，并同步最新的代理设置（代理池是就地刷新的）"""
        if self._media_downloader is None:
            self._media_downloader = MediaDownloader(
                platform="xhs",
                proxy=getattr(self.xhs_client, "proxy", None),
                extra_headers=self._media_headers(),
            )
        else:
            self._media_downloader.update_credentials(
                proxy=getattr(self.xhs_client, "proxy", None),
                extra_headers=self._media_headers(),
            )
        return self._media_downloader

    def _media_headers(self) -> Dict:
        """媒体请求头：平台 Referer（防盗链）+ UA"""
        return {
            "Referer": "https://www.xiaohongshu.com/",
            "User-Agent": self.user_agent,
        }
