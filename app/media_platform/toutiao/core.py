# -*- coding: utf-8 -*-
# 今日头条（头条号）爬虫：浏览器页面驱动，登录/游客双模式，安全保守节奏
import asyncio
import math
import os
import random
from typing import Dict, List, Optional
from urllib.parse import quote

from patchright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    async_playwright,
)

import config
from base.base_crawler import AbstractCrawler
from tools import qr_state, utils, browser_launch
from tools.login_state import mark_logged_in
from tools.async_file_writer import AsyncFileWriter
from var import crawler_type_var, source_keyword_var

from .field import CAPTURE_HOOK_JS, TYPE_ARTICLE
from .help import (
    extract_group_id,
    extract_user_token,
    judge_content_type,
    normalize_article_url,
)
from .login import ToutiaoLogin, _LOGIN_COOKIE_KEYS


class ToutiaoCrawler(AbstractCrawler):
    context_page: Page
    browser_context: BrowserContext

    def __init__(self) -> None:
        self.index_url = "https://www.toutiao.com"
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0.0 Safari/537.36"
        )
        # 延迟到 start() 里 crawler_type 确定后再创建，避免文件名前缀为空
        self.writer: Optional[AsyncFileWriter] = None

    # ---------- 主流程 ----------
    async def start(self) -> None:
        async with async_playwright() as playwright:
            chromium = playwright.chromium
            self.browser_context = await self.launch_browser(
                chromium, None, self.user_agent, headless=config.HEADLESS
            )
            # 反指纹 + 接口响应拦截（均在页面脚本前注入）
            await self.browser_context.add_init_script(path="libs/stealth.min.js")
            await self.browser_context.add_init_script(CAPTURE_HOOK_JS)

            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url, wait_until="domcontentloaded")

            if config.CRAWL_MODE == "guest":
                utils.logger.info(
                    "[ToutiaoCrawler.start] Guest mode enabled, skip login and crawl public content only"
                )
            else:
                _logged = await self._is_logged_in()
                utils.logger.info("[ToutiaoCrawler.start] already logged in = %s", _logged)
                if _logged:
                    # 已具备登录态，无需扫码：写 success 让前端自动关闭浮层
                    qr_state.write_qr_state(config.PLATFORM, qr_state.STATUS_SUCCESS)
                    mark_logged_in(config.PLATFORM)
                else:
                    login_obj = ToutiaoLogin(
                        login_type=config.LOGIN_TYPE,
                        login_phone="",
                        browser_context=self.browser_context,
                        context_page=self.context_page,
                        cookie_str=config.COOKIES,
                    )
                    await login_obj.begin()
                    mark_logged_in(config.PLATFORM)

            if getattr(config, "LOGIN_ONLY", False):
                utils.logger.info("[Crawler.start] login-only mode, finished after login, skip crawling")
                return
            crawler_type_var.set(config.CRAWLER_TYPE)
            self.writer = AsyncFileWriter(
                platform="toutiao", crawler_type=config.CRAWLER_TYPE
            )
            if config.CRAWLER_TYPE == "search":
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                await self.get_creators_and_notes()
            utils.logger.info("[ToutiaoCrawler.start] Toutiao Crawler finished ...")

    async def _is_logged_in(self) -> bool:
        cookies = await self.browser_context.cookies()
        _, cookie_dict = utils.convert_cookies(cookies)
        return any(cookie_dict.get(k) for k in _LOGIN_COOKIE_KEYS)

    # ---------- 安全节奏 ----------
    async def _human_pause(self, mn: Optional[float] = None, mx: Optional[float] = None) -> None:
        lo = mn if mn is not None else config.CRAWLER_SLEEP_MIN
        hi = mx if mx is not None else config.CRAWLER_SLEEP_MAX
        await asyncio.sleep(random.uniform(lo, hi))

    async def _reset_capture(self) -> None:
        await self.context_page.evaluate(
            "window.__TT_CAPTURED__={feed:[],comments:[],search:[]}"
        )

    async def _wait_capture(self, bucket: str, timeout: int = 12) -> bool:
        elapsed = 0
        page = self.context_page
        while elapsed < timeout:
            n = await page.evaluate(
                "(b)=>window.__TT_CAPTURED__[b].length", bucket
            )
            if n:
                return True
            await asyncio.sleep(1)
            elapsed += 1
        return False

    async def _drain(self, bucket: str) -> List[Dict]:
        return (
            await self.context_page.evaluate(
                "(b)=>{const a=window.__TT_CAPTURED__[b];window.__TT_CAPTURED__[b]=[];return a;}",
                bucket,
            )
        ) or []

    async def _scroll(self, ratio: float = 0.85) -> None:
        await self.context_page.evaluate(
            "(r)=>window.scrollBy(0,Math.floor(window.innerHeight*r))", ratio
        )

    # ---------- 搜索模式 ----------
    async def search(self) -> None:
        utils.logger.info("[ToutiaoCrawler.search] Begin search toutiao keywords")
        page = self.context_page
        target = config.CRAWLER_MAX_NOTES_COUNT
        max_rounds = max(4, math.ceil(target / 10) + 3)

        for keyword in config.KEYWORDS.split(","):
            keyword = keyword.strip()
            if not keyword:
                continue
            source_keyword_var.set(keyword)
            utils.logger.info(f"[ToutiaoCrawler.search] Current keyword: {keyword}")
            await self._reset_capture()
            await page.goto(
                f"https://so.toutiao.com/search?keyword={quote(keyword)}&pd=information",
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(4)

            gids: List[str] = []
            rounds = 0
            while len(gids) < target and rounds < max_rounds:
                hrefs = await page.evaluate("()=>[...document.querySelectorAll('a')].map(a=>a.href)")
                for h in hrefs:
                    gid = extract_group_id(h)
                    if gid and gid not in gids:
                        gids.append(gid)
                if len(gids) >= target:
                    break
                await self._scroll()
                await self._human_pause()
                rounds += 1

            for gid in gids[:target]:
                note_url = f"https://www.toutiao.com/article/{gid}/"
                await self._reset_capture()
                await page.goto(note_url, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                content = await self._parse_article_page(gid, note_url)
                content["source_keyword"] = keyword
                await self.writer.write_to_jsonl(content, "contents")
                if config.ENABLE_GET_COMMENTS:
                    await self._crawl_comments(gid, note_url)

    # ---------- 链接(详情)模式 ----------
    async def get_specified_notes(self) -> None:
        page = self.context_page
        for url in config.TOUTIAO_SPECIFIED_ID_LIST:
            gid = extract_group_id(url)
            note_url = normalize_article_url(url, gid)
            if not note_url:
                continue
            await self._reset_capture()
            await page.goto(note_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            content = await self._parse_article_page(gid, note_url)
            await self.writer.write_to_jsonl(content, "contents")
            if config.ENABLE_GET_COMMENTS:
                await self._crawl_comments(gid, note_url)

    # ---------- 头条号(创作者)模式 ----------
    async def get_creators_and_notes(self) -> None:
        page = self.context_page
        for home_url in config.TOUTIAO_CREATOR_URL_LIST:
            await self._reset_capture()
            await page.goto(home_url, wait_until="domcontentloaded")
            await self._wait_capture("feed", timeout=15)

            collected: Dict[str, Dict] = {}
            has_more = True
            rounds = 0
            max_rounds = max(2, math.ceil(config.CRAWLER_MAX_NOTES_COUNT / 20) + 2)

            while has_more and rounds < max_rounds and len(collected) < config.CRAWLER_MAX_NOTES_COUNT:
                await self._wait_capture("feed", timeout=12)
                entries = await self._drain("feed")
                for entry in entries:
                    j = entry.get("data", {})
                    has_more = bool(j.get("has_more"))
                    for d in j.get("data", []):
                        gid = str(d.get("group_id") or "")
                        if gid:
                            collected[gid] = d
                await self._human_pause()
                await self._scroll()
                rounds += 1

            note_urls: Dict[str, str] = {}
            for d in list(collected.values())[: config.CRAWLER_MAX_NOTES_COUNT]:
                content = self._build_content_from_feed(d, "")
                note_urls[content["note_id"]] = content["note_url"]
                await self.writer.write_to_jsonl(content, "contents")

            if config.ENABLE_GET_COMMENTS:
                for gid, note_url in note_urls.items():
                    await self._crawl_comments(gid, note_url)

    # ---------- 正文解析 ----------
    async def _parse_article_page(self, gid: str, note_url: str) -> Dict:
        data = await self.context_page.evaluate(
            """()=>{
              const root=document.querySelector('.article-content');
              if(!root) return {title:document.title,text:'',time:null,author:''};
              const h1=root.querySelector('h1');
              const ps=[...root.querySelectorAll('article p')]
                .map(p=>p.textContent.trim()).filter(Boolean);
              const alink=[...root.querySelectorAll('a')].find(a=>/\\/c\\/user\\//.test(a.href));
              let time=null,bestLen=1e9;
              for(const e of root.querySelectorAll('*')){
                const t=e.textContent.trim();
                const m=t.match(/(\\d{4}-\\d{2}-\\d{2})\\s+(\\d{2}:\\d{2})/);
                if(m&&t.length<bestLen){
                  const ms=new Date(m[1]+' '+m[2]).getTime();
                  if(!isNaN(ms)){time=Math.floor(ms/1000);bestLen=t.length;}
                }
              }
              return {
                title:h1?h1.textContent.trim():document.title,
                text:ps.join('\\n'),
                time:time,
                author:alink?alink.textContent.trim():''
              };
            }"""
        )
        return {
            "note_id": gid,
            "title": data.get("title", ""),
            "desc": data.get("text", ""),
            "type": TYPE_ARTICLE,
            "note_url": note_url,
            "nickname": data.get("author", ""),
            "time": data.get("time"),
            "source_keyword": "",
            "last_modify_ts": utils.get_current_timestamp(),
        }

    def _extract_images(self, item: Dict) -> List[str]:
        for key in ("large_image_list", "image_list", "middle_image"):
            arr = item.get(key)
            if isinstance(arr, list) and arr:
                urls: List[str] = []
                for x in arr:
                    if isinstance(x, dict):
                        u = x.get("url") or x.get("url_default")
                        if u:
                            urls.append(u)
                    elif isinstance(x, str):
                        urls.append(x)
                if urls:
                    return urls
        return []

    def _build_content_from_feed(self, item: Dict, source_keyword: str) -> Dict:
        ui = item.get("user_info") or {}
        gid = str(item.get("group_id") or item.get("item_id_str") or "")
        return {
            "note_id": gid,
            "title": item.get("title", ""),
            "desc": item.get("abstract", ""),
            "type": judge_content_type(item),
            "note_url": normalize_article_url(
                item.get("article_url") or item.get("display_url"), gid
            ),
            "time": item.get("publish_time") or item.get("behot_time"),
            "nickname": ui.get("name", ""),
            "creator_hash": str(ui.get("user_id", "")),
            "liked_count": item.get("digg_count", 0),
            "comment_count": item.get("comment_count", 0),
            "collected_count": item.get("repin_count", 0),
            "read_count": item.get("read_count", 0),
            "share_count": item.get("share_count", 0),
            "image_list": self._extract_images(item),
            "source_keyword": source_keyword or "",
            "last_modify_ts": utils.get_current_timestamp(),
        }

    # ---------- 评论抓取 ----------
    async def _crawl_comments(self, gid: str, note_url: str) -> None:
        page = self.context_page
        if gid not in page.url:
            # 仅在需要跳转时重置；跳转后首屏评论会被 hook 自动捕获
            await self._reset_capture()
            await page.goto(note_url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

        # 首屏 tab_comments 已由 hook 自动捕获，不要主动清空
        await self._wait_capture("comments", timeout=10)

        # 打开“查看全部”评论抽屉（头条 PC 端评论在独立 ttp-drawer 内）
        await self._open_comment_drawer()

        total = 0
        idle = 0
        seen: set = set()
        limit = config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES
        while total < limit and idle < 5:
            entries = await self._drain("comments")  # 读取并清空已处理部分
            grew = 0
            for entry in entries:
                for cm in self._parse_comments(entry.get("data", {}), gid, note_url):
                    cid = cm.get("comment_id")
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    await self.writer.write_to_jsonl(cm, "comments")
                    grew += 1
            total += grew

            # 滚动最后一项到可视并点击“查看更多评论”，触发下一页带签名的 tab_comments
            before_count = await self._comment_item_count()
            advanced = await self._advance_comments()
            await self._wait_capture("comments", timeout=8)
            after_count = await self._comment_item_count()
            if after_count > before_count or grew or advanced:
                idle = 0
            else:
                idle += 1
            await self._human_pause()

        utils.logger.info(f"[ToutiaoCrawler._crawl_comments] gid={gid} comments={total}")
        await self._close_comment_drawer()

    async def _comment_item_count(self) -> int:
        return await self.context_page.locator(
            ".comment-list .ttp-comment-item"
        ).count()

    async def _open_comment_drawer(self) -> None:
        clicked = await self.context_page.evaluate(
            """()=>{
              const b=[...document.querySelectorAll('button')]
                .find(e=>e.offsetParent!==null&&/查看全部/.test(e.textContent));
              if(b){b.click();return true;}
              return false;
            }"""
        )
        if clicked:
            await asyncio.sleep(3)
            # 中间可能出现“打开评论面板”按钮，再点一次
            await self.context_page.evaluate(
                """()=>{
                  const b=[...document.querySelectorAll('button')]
                    .find(e=>e.offsetParent!==null&&/打开评论面板/.test(e.textContent));
                  if(b){b.click();return true;}
                  return false;
                }"""
            )
            await asyncio.sleep(2)

    async def _advance_comments(self) -> bool:
        """推进评论抽屉一页：滚动最后一项到可视，再点击底部“查看更多评论”。"""
        page = self.context_page
        loc = page.locator(".comment-list .ttp-comment-item")
        n = await loc.count()
        if not n:
            return False
        last = loc.nth(n - 1)
        try:
            # 滚动最后一项到可视，Playwright 自动驱动正确的可滚动祖先
            await last.scroll_into_view_if_needed(timeout=5000)
            await last.hover()
            await page.mouse.wheel(0, 800)
            await asyncio.sleep(1.5)
            # 抽屉底部出现“查看更多评论”按钮，点击才会发下一页 tab_comments
            more = page.locator("button", has_text="查看更多评论")
            if await more.count():
                await more.first.click(timeout=5000)
                return True
        except Exception as e:  # 元素状态变化不应中断整体流程
            utils.logger.warning(f"[ToutiaoCrawler._advance_comments] warn: {e}")
        return False

    async def _close_comment_drawer(self) -> None:
        await self.context_page.evaluate(
            """()=>{
              const d=document.querySelector('.ttp-drawer');
              if(!d) return;
              const x=d.querySelector('[class*=close],[aria-label=关闭],button');
              if(x&&/关闭|close/.test((x.getAttribute('aria-label')||'')+x.className)) x.click();
            }"""
        )

    def _parse_comments(self, j: Dict, gid: str, note_url: str) -> List[Dict]:
        out: List[Dict] = []
        for wrapper in j.get("data", []):
            c = wrapper.get("comment") if isinstance(wrapper, dict) else None
            if not c:
                continue
            main = self._build_comment(c, gid, note_url, "")
            out.append(main)
            replies = c.get("reply_list") or c.get("new_reply_list") or []
            for r in replies:
                rc = r.get("comment") if isinstance(r, dict) and "comment" in r else r
                if isinstance(rc, dict):
                    out.append(self._build_comment(rc, gid, note_url, main["comment_id"]))
        return out

    def _build_comment(
        self, c: Dict, gid: str, note_url: str, parent_id: str
    ) -> Dict:
        user = c.get("user") or {}
        return {
            "comment_id": str(c.get("id_str") or c.get("id") or ""),
            "note_id": gid,
            "content": c.get("text", ""),
            "nickname": c.get("user_name") or user.get("name", ""),
            "creator_hash": str(c.get("user_id") or user.get("user_id", "")),
            "like_count": c.get("digg_count", 0),
            "create_time": c.get("create_time"),
            "sub_comment_count": c.get("reply_count", 0),
            "parent_comment_id": parent_id,
            "note_url": note_url,
            "last_modify_ts": utils.get_current_timestamp(),
        }

    # ---------- 浏览器启动 ----------
    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        utils.logger.info(
            "[ToutiaoCrawler.launch_browser] Begin create browser context ..."
        )
        if config.CRAWL_MODE == "guest":
            profile_dir = os.path.join(
                os.getcwd(), "browser_data", f"{config.PLATFORM}_guest_user_data_dir"
            )
            utils.logger.info("[ToutiaoCrawler.launch_browser] Guest mode, using isolated guest profile")
        else:
            profile_dir = os.path.join(
                os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM
            )
        # 头条存在无头检测（无头访问返回空页面/风控）。对齐 xhs/dy/wb 重点平台策略：
        # Patchright 驱动系统真实 Edge + 离屏有头窗口，视觉静默且降低被识别风险（见 browser_launch）。
        return await chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            accept_downloads=True,
            headless=browser_launch.effective_headless(config.PLATFORM, headless),
            args=browser_launch.offscreen_args(config.PLATFORM),
            channel=browser_launch.browser_channel(config.PLATFORM),
            proxy=playwright_proxy,
            viewport={"width": 1440, "height": 900},
            user_agent=user_agent,
        )
