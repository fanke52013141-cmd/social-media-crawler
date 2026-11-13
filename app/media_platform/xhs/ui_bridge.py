# -*- coding: utf-8 -*-
"""UI 驱动采集桥接层（小红书）。

设计目标：让每一次数据获取都伴随真实的页面加载、阅读停留、滚动与点击，
使服务端看到的行为链路（页面遥测 + 数据请求）与真人一致，而不是"纯接口爬虫"。

实现方式：
- 真实打开搜索页/笔记详情页/评论区，数据取自**页面自身发出的 API 响应**
  （response 拦截），JSON 结构与直连 API 完全一致 → 存储/导出/字段零变化。
- 独立搜索标签页保持翻页状态（详情页跳转不打断搜索会话）。
- 阅读停留按内容长度计算 + 拟人化滚动/鼠标动作（复用 HumanRhythm）。
- 风控信号（429/300011/461/471 等）与 API 路径同等处理：熔断/过码/冷却。
- 任何 UI 异常由调用方（client.py）回退到原 API 路径，功能保全。

不占用屏幕：全程运行在无头浏览器中；仅验证码时经 captcha_handler 弹小窗。
"""
import asyncio
import random
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

from patchright.async_api import BrowserContext, Page

import config
from tools import utils, risk_signals
from tools.humanize import HumanRhythm
from tools.human_mouse import click_human

from .exception import (
    CaptchaError,
    DataFetchError,
    IPBlockError,
    NoteNotFoundError,
    PlatformAccessError,
    RiskControlError,
)
from .extractor import XiaoHongShuExtractor
from .field import SearchSortType

SEARCH_PATH = "/api/sns/web/v2/search/notes"
FEED_PATH = "/api/sns/web/v1/feed"
COMMENT_PATH = "/api/sns/web/v2/comment/page"
SUB_COMMENT_PATH = "/api/sns/web/v2/comment/sub/page"


class XhsUIBridge:
    """把 client 的取数方法映射为真实页面操作 + 响应拦截。"""

    def __init__(self, context: BrowserContext, main_page: Page,
                 index_url: str, captcha_handler=None) -> None:
        self.context = context
        self.page = main_page            # 详情/评论页（主页面）
        self.index_url = index_url
        self.captcha_handler = captcha_handler
        self._search_page: Optional[Page] = None   # 独立搜索标签页
        self._search_keyword: Optional[str] = None
        self._search_queue: asyncio.Queue = asyncio.Queue()
        self._feed_queue: asyncio.Queue = asyncio.Queue()
        self._comment_queue: asyncio.Queue = asyncio.Queue()
        self._sub_queue: asyncio.Queue = asyncio.Queue()
        # 详情页阅读期间顺带捕获的首批评论，评论阶段直接复用，避免重复加载
        self._comment_cache: Dict[str, List[Dict]] = {}
        self._rhythm = HumanRhythm(main_page)
        self._extractor = XiaoHongShuExtractor()
        self._attach(self.page, {
            FEED_PATH: self._feed_queue,
            COMMENT_PATH: self._comment_queue,
            SUB_COMMENT_PATH: self._sub_queue,
        })

    # ---------- 响应拦截 ----------
    def _attach(self, page: Page, queue_map: Dict[str, asyncio.Queue]) -> None:
        async def on_response(resp) -> None:
            try:
                path = urlparse(resp.url).path
                q = queue_map.get(path)
                if q is None:
                    return
                data = None
                try:
                    data = await resp.json()
                except Exception:
                    pass
                await q.put((resp.status, dict(resp.headers), data))
            except Exception:
                return
        page.on("response", on_response)

    @staticmethod
    def _drain(q: asyncio.Queue) -> List:
        items = []
        while not q.empty():
            try:
                items.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def _check(self, status: int, headers: Dict, data: Optional[Dict]):
        """与 client.request 同等语义的风控/错误处理，返回 data 字段。"""
        if status in (401, 403):
            raise PlatformAccessError(f"XHS UI response blocked with HTTP {status}")
        if status == 429:
            risk_signals.record(config.PLATFORM, risk_signals.KIND_RATE_LIMIT,
                                "UI HTTP 429 circuit breaker")
            raise RiskControlError("XHS rate limited (UI HTTP 429), abort for cooldown")
        if status in (461, 471):
            verify_type = headers.get("verifytype", "")
            verify_uuid = headers.get("verifyuuid", "")
            risk_signals.record(config.PLATFORM, risk_signals.KIND_CAPTCHA,
                                f"UI HTTP {status} Verifytype {verify_type}")
            if self.captcha_handler is not None:
                await self.captcha_handler(verify_type, verify_uuid)
                return None  # 过码后页面会自动重新加载数据，调用方继续等待
            raise CaptchaError(f"CAPTCHA required (UI), Verifytype: {verify_type}")
        if not isinstance(data, dict):
            raise DataFetchError(f"UI response not JSON, HTTP {status}")
        code = str(data.get("code")) if data.get("code") is not None else ""
        if code == "300012":
            raise IPBlockError("Network connection error (UI)")
        if code == "300011":
            risk_signals.record(config.PLATFORM, risk_signals.KIND_SECURITY,
                                "UI code 300011 circuit breaker")
            raise RiskControlError("XHS account security restriction (UI 300011)")
        if data.get("success"):
            return data.get("data", data.get("success", {}))
        if code in ("-510000", "-510001"):
            raise NoteNotFoundError(f"Note not found or abnormal (UI), code: {code}")
        raise DataFetchError(data.get("msg") or f"UI data fetch failed, code={code}")

    async def _wait_resp(self, q: asyncio.Queue, timeout: float):
        """等待一条被拦截的响应并做风控检查；超时返回 None；过码后继续等。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remain = deadline - asyncio.get_event_loop().time()
            if remain <= 0:
                return None
            try:
                status, headers, data = await asyncio.wait_for(q.get(), remain)
            except asyncio.TimeoutError:
                return None
            result = await self._check(status, headers, data)
            if result is None and status in (461, 471):
                continue  # 已过码，等新数据
            return result

    # ---------- 搜索 ----------
    async def _ensure_search_page(self) -> Page:
        if self._search_page is None or self._search_page.is_closed():
            self._search_page = await self.context.new_page()
            self._attach(self._search_page, {SEARCH_PATH: self._search_queue})
        return self._search_page

    async def search_notes(self, keyword: str, page: int,
                           sort: SearchSortType) -> Dict:
        """真实搜索页 + 滚动翻页，返回与 API 同构的 {has_more, items}。"""
        sp = await self._ensure_search_page()
        rhythm = HumanRhythm(sp)
        if keyword != self._search_keyword or page <= 1:
            self._search_keyword = keyword
            self._drain(self._search_queue)
            url = (f"{self.index_url}/search_result?keyword={quote(keyword)}"
                   f"&source=web_explore_feed")
            await sp.goto(url, wait_until="domcontentloaded")
            result = await self._wait_resp(self._search_queue, 12)
            if result is None:
                # 页面未自发搜索请求（SSR/渲染延迟）：在搜索框真实回车重搜，
                # 触发客户端自身的搜索 XHR
                try:
                    box = sp.locator(
                        "input#search-input, input[placeholder*='搜索'], "
                        ".search-input input, input.search-input").first
                    await click_human(sp, box, timeout=5000)
                    await box.press("Enter")
                    result = await self._wait_resp(self._search_queue, 12)
                except Exception as e:
                    utils.logger.warning(f"[UIBridge] 回车重搜失败: {e}")
            if sort == SearchSortType.LATEST:
                # 点击「最新」排序标签，触发页面自己按时间排序重新搜索
                try:
                    tab = sp.get_by_text("最新", exact=True).first
                    await click_human(sp, tab, timeout=8000)
                    latest = await self._wait_resp(self._search_queue, 20)
                    if latest is not None:
                        result = latest
                except Exception as e:
                    utils.logger.warning(f"[UIBridge] 点击「最新」排序失败，沿用当前排序: {e}")
            if result is None:
                # SSR 兜底：搜索页首屏可能走 SSR 不发 /search/notes XHR，
                # 从 __INITIAL_STATE__.search 取首屏结果（主世界执行）。
                result = await self._ssr_search_notes(sp)
            if result is None:
                raise DataFetchError(f"UI search no response for keyword={keyword}")
            await rhythm.human_pause(1.5, 4)
            return result
        # 后续页：拟人化滚动到底部，触发页面自身的下一页加载
        await rhythm.browse_step()
        await sp.mouse.wheel(0, random.randint(2500, 4000))
        result = await self._wait_resp(self._search_queue, 20)
        if result is None:
            return {"has_more": False, "items": []}
        await rhythm.human_pause(1.0, 3)
        return result

    async def _ssr_search_notes(self, sp: Page) -> Optional[Dict]:
        """搜索页 SSR 兜底：从 __INITIAL_STATE__.search.feeds 提取首屏结果（主世界执行）。
        真实结构（实测）：search.feeds 数组，项含 id/modelType/noteCard/xsecToken（camelCase）。
        取不到返回 None，调用方回退 API；结构不匹配时优雅降级，不影响采集。"""
        try:
            raw = await sp.evaluate(
                """() => {
                  const uw = v => (v && (v.__v_isRef || v.__v_isShallow))
                      ? (v._value !== undefined ? v._value : v._rawValue) : v;
                  const st = window.__INITIAL_STATE__;
                  const s = st && uw(st.search);
                  if (!s) return null;
                  const list = uw(s.feeds);
                  if (!Array.isArray(list) || !list.length) return null;
                  const items = [];
                  for (const it of list) {
                    const n = uw(it);
                    if (!n || !n.id) continue;
                    if (n.modelType && n.modelType !== 'note') continue;
                    items.push({id: n.id, model_type: 'note',
                                note_card: uw(n.noteCard) || null,
                                xsec_token: n.xsecToken || ''});
                  }
                  if (!items.length) return null;
                  return {has_more: !!s.hasMore, items: items};
                }""",
                isolated_context=False,
            )
            if raw and raw.get("items"):
                import humps
                for it in raw["items"]:
                    if it.get("note_card"):
                        it["note_card"] = humps.decamelize(it["note_card"])
                return raw
        except Exception as e:
            utils.logger.warning(f"[UIBridge] SSR 搜索提取异常: {e}")
        return None

    # ---------- 笔记详情 ----------
    async def note_detail(self, note_id: str, xsec_source: str,
                          xsec_token: str) -> Dict:
        """真实打开笔记详情页，按内容长度阅读停留，返回 note_card（与 API 同构）。"""
        self._drain(self._feed_queue)
        self._drain(self._comment_queue)
        url = (f"{self.index_url}/explore/{note_id}"
               f"?xsec_token={quote(xsec_token or '')}"
               f"&xsec_source={xsec_source or 'pc_search'}")
        await self.page.goto(url, wait_until="domcontentloaded")
        # 双通道取数：① 页面自身 /feed XHR（从列表点进详情的路径）；
        # ② 直接 URL 打开时小红书走 SSR，数据内嵌 __INITIAL_STATE__、不发 /feed，
        #    此时解析 SSR（与 client.get_note_by_id_from_html 同源同结构）。
        result = await self._wait_resp(self._feed_queue, 8)
        note_card = None
        if result and result.get("items"):
            note_card = result["items"][0]["note_card"]
        else:
            try:
                # 页内直接序列化 __INITIAL_STATE__（避开正则+json.loads 对
                # undefined/多 script 片段的解析失败），再 decamelize 对齐 API 字段
                raw = await self.page.evaluate(
                    """(nid) => {
                      const uw = v => (v && (v.__v_isRef || v.__v_isShallow))
                          ? (v._value !== undefined ? v._value : v._rawValue) : v;
                      const st = window.__INITIAL_STATE__;
                      if (!st || !st.note) return null;
                      const m = uw(st.note.noteDetailMap || st.note.note_detail_map);
                      if (!m) return null;
                      const e = uw(m[nid]);
                      const note = e ? uw(e.note) : null;
                      return note ? JSON.parse(JSON.stringify(note)) : null;
                    }""",
                    note_id,
                    isolated_context=False,
                )
                if raw:
                    import humps
                    note_card = humps.decamelize(raw)
            except Exception as e:
                utils.logger.warning(f"[UIBridge] SSR 解析异常 note={note_id}: {e}")
        if not note_card:
            raise DataFetchError(f"UI feed empty for note {note_id}")
        await self._read_dwell(note_card, note_id)
        return note_card

    async def _read_dwell(self, note_card: Dict, note_id: str) -> None:
        """按内容长度计算阅读停留，期间拟人化滚动，并缓存顺带加载的评论。"""
        text_len = len(note_card.get("title") or "") + len(note_card.get("desc") or "")
        # 真人阅读速度约 5–9 字/秒，取中值并加抖动；短帖至少停 4s，长帖封顶 40s
        base = min(40.0, max(4.0, text_len / 7.0))
        dwell = base * random.uniform(0.7, 1.5)
        steps = random.randint(2, 4)
        cached: List[Dict] = []
        for i in range(steps):
            await self._rhythm.browse_step()
            await asyncio.sleep(dwell / steps)
            for status, headers, data in self._drain(self._comment_queue):
                try:
                    payload = await self._check(status, headers, data)
                    if payload:
                        cached.append(payload)
                except (RiskControlError, CaptchaError, IPBlockError,
                        PlatformAccessError):
                    raise
                except Exception:
                    continue
        if cached:
            self._comment_cache[note_id] = cached

    # ---------- 评论 ----------
    async def _ssr_first_comments(self, note_id: str) -> Optional[Dict]:
        """从 SSR __INITIAL_STATE__ 提取首屏一级评论（与 comment/page 同构）。
        取不到返回 None；Vue ref 做一层解包。"""
        try:
            return await self.page.evaluate(
                """(nid) => {
                  const uw = v => (v && (v.__v_isRef || v.__v_isShallow))
                      ? (v._value !== undefined ? v._value : v._rawValue) : v;
                  const st = window.__INITIAL_STATE__;
                  if (!st || !st.note) return null;
                  const m = uw(st.note.noteDetailMap || st.note.note_detail_map);
                  if (!m) return null;
                  const e = uw(m[nid]);
                  if (!e) return null;
                  const cwrap = uw(e.comments);
                  const cands = [
                    Array.isArray(cwrap) ? cwrap : null,
                    cwrap && Array.isArray(uw(cwrap.list)) ? uw(cwrap.list) : null,
                    Array.isArray(uw(e.commentList)) ? uw(e.commentList) : null,
                  ];
                  for (const c of cands) {
                    if (c && c.length && c[0] && uw(c[0]).id) {
                      const list = c.map(uw);
                      const hm = cwrap && !Array.isArray(cwrap)
                          ? (cwrap.has_more !== undefined ? cwrap.has_more
                             : cwrap.hasMore) : e.commentsHasMore;
                      const cur = cwrap && !Array.isArray(cwrap)
                          ? (cwrap.cursor || '') : (e.commentsCursor || '');
                      return {comments: list, has_more: !!hm, cursor: cur || ''};
                    }
                  }
                  return null;
                }""",
                note_id,
                isolated_context=False,
            )
        except Exception as e:
            utils.logger.warning(f"[UIBridge] SSR 首屏评论提取异常 note={note_id}: {e}")
            return None

    async def _scroll_comments(self) -> None:
        """在笔记页向下滚动到评论区并继续滚动，触发评论分页加载。"""
        await self._rhythm.browse_step()
        await self.page.mouse.wheel(0, random.randint(1800, 3200))

    async def comment_batches(self, note_id: str, xsec_token: str):
        """异步生成器：yield (kind, payload)，kind ∈ {"root", "sub"}。
        payload 与 comment/page、comment/sub/page 的 data 同构。"""
        # 1) 复用详情页阅读期间缓存的首批评论
        cached = self._comment_cache.pop(note_id, [])
        for payload in cached:
            yield "root", payload
            if not payload.get("has_more", False):
                return
        # 2) 当前不在该笔记页则真实打开
        if note_id not in (self.page.url or ""):
            self._drain(self._comment_queue)
            url = (f"{self.index_url}/explore/{note_id}"
                   f"?xsec_token={quote(xsec_token or '')}&xsec_source=pc_search")
            await self.page.goto(url, wait_until="domcontentloaded")
            await self._wait_resp(self._feed_queue, 8)
            await asyncio.sleep(random.uniform(1.5, 3.5))
        # 2.5) SSR 首屏评论：直接 URL 打开时首批评论内嵌页面、不发 XHR。
        # 若页面是 SSR 却取不到首屏，宁可抛错回退 API，也不静默丢首页评论。
        if not cached:
            ssr = await self._ssr_first_comments(note_id)
            if ssr is not None:
                yield "root", ssr
                if not ssr.get("has_more", False):
                    return
            else:
                try:
                    html = await self.page.content()
                except Exception:
                    html = ""
                if "noteDetailMap" in html:
                    raise DataFetchError(
                        f"UI comments: SSR first page unreadable for {note_id}, fallback API")
        # 3) 滚动加载更多一级评论
        no_new = 0
        while no_new < 3:
            await self._scroll_comments()
            payload = await self._wait_resp(self._comment_queue, 8)
            if payload is None:
                no_new += 1
                continue
            no_new = 0
            yield "root", payload
            if not payload.get("has_more", False):
                break
        # 4) 子评论：真实点击「展开更多评论/展开X条回复」按钮
        if getattr(config, "ENABLE_GET_SUB_COMMENTS", False):
            expand = self.page.locator(
                "text=/展开更多评论|展开\\s*\\d+\\s*条回复|查看更多评论/")
            for _ in range(40):  # 防御性上限，正常由按钮耗尽退出
                try:
                    count = await expand.count()
                except Exception:
                    break
                clicked = False
                for i in range(count):
                    btn = expand.nth(i)
                    try:
                        if not await btn.is_visible():
                            continue
                        await btn.scroll_into_view_if_needed(timeout=3000)
                        await self._rhythm.human_action()
                        await click_human(self.page, btn, timeout=5000)
                        clicked = True
                        payload = await self._wait_resp(self._sub_queue, 8)
                        if payload is not None:
                            yield "sub", payload
                        await asyncio.sleep(random.uniform(0.8, 2.0))
                        break  # DOM 已变化，重新枚举
                    except Exception:
                        continue
                if not clicked:
                    break

    async def close(self) -> None:
        try:
            if self._search_page is not None and not self._search_page.is_closed():
                await self._search_page.close()
        except Exception:
            pass

    def rebind(self, context: BrowserContext, main_page: Page) -> None:
        """验证码处理会整体重建浏览器 context，此处重绑页面与拦截器。"""
        self.context = context
        self.page = main_page
        self._search_page = None
        self._search_keyword = None
        self._comment_cache.clear()
        for q in (self._search_queue, self._feed_queue,
                  self._comment_queue, self._sub_queue):
            self._drain(q)
        self._rhythm = HumanRhythm(main_page)
        self._attach(main_page, {
            FEED_PATH: self._feed_queue,
            COMMENT_PATH: self._comment_queue,
            SUB_COMMENT_PATH: self._sub_queue,
        })