# -*- coding: utf-8 -*-
# 今日头条辅助函数：URL 解析、类型判定
import re
from urllib.parse import unquote, parse_qs, urlparse

from .field import TYPE_ARTICLE, TYPE_VIDEO, TYPE_MICRO

# 匹配头条文章/视频/小组链接里的数字 ID
_ID_PATTERN = re.compile(r"/(?:article|group|video|w)/(\d+)")
# 旧版头条号主页
_USER_PATTERN = re.compile(r"/c/user/(\d+)")
# 新版 token 主页
_TOKEN_PATTERN = re.compile(r"/c/user/token/([^/?#]+)")


def extract_group_id(url: str) -> str:
    """从文章/视频链接中提取数字 group_id；提取不到返回空串。"""
    if not url:
        return ""
    m = _ID_PATTERN.search(url)
    if m:
        return m.group(1)
    # 搜索 jump 链接把真实 URL 放在 url 参数里
    if "/search/jump" in url:
        try:
            qs = parse_qs(urlparse(url).query)
            inner = qs.get("url", [""])[0]
            inner = unquote(inner)
            m2 = _ID_PATTERN.search(inner)
            if m2:
                return m2.group(1)
            gid = parse_qs(urlparse(inner).query).get("groupid", [""])[0]
            if gid.isdigit():
                return gid
        except Exception:
            return ""
    return ""


def extract_user_token(url: str) -> str:
    """从头条号主页链接提取 token 或旧版数字 ID。"""
    if not url:
        return ""
    m = _TOKEN_PATTERN.search(url)
    if m:
        return m.group(1)
    m = _USER_PATTERN.search(url)
    if m:
        return m.group(1)
    return url.rstrip("/").split("/")[-1]


def is_creator_home_url(url: str) -> bool:
    return "/c/user/" in url


def judge_content_type(item: dict) -> str:
    """根据列表项字段判定内容类型。"""
    try:
        if item.get("has_video") or item.get("video_detail_info"):
            return TYPE_VIDEO
        cell_type = item.get("cell_type")
        # 微头条 cell_type 通常为 0/14 且无独立文章页，这里结合 article_url 判定
        if cell_type in (0, 14) and not item.get("article_url"):
            return TYPE_MICRO
    except Exception:
        pass
    return TYPE_ARTICLE


def normalize_article_url(url: str, group_id: str) -> str:
    """统一生成可访问的文章链接。"""
    if url and url.startswith("http"):
        return url
    if group_id:
        return f"https://www.toutiao.com/article/{group_id}/"
    return ""
