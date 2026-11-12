# -*- coding: utf-8 -*-
# 定时任务多 sheet 汇总导出器。
# 产出：一个 Excel，每个参与平台一个 sheet（sheet 名=平台中文名）。
# 各平台原始字段名不同，先按平台映射到统一标准字段，再统一转中文列；并严格按时间窗过滤。
import glob
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from tools import task_store, time_util
from tools.async_file_writer import data_dir_of

# ---------------- 平台元数据：主键 / 时间 / 链接字段，以及原始字段 -> 标准字段 ----------------
PLATFORM_META = {
    "xhs": {
        "name": "小红书",
        "content": {
            "note_id": "id", "type": "item_type", "title": "title", "desc": "desc",
            "time": "pub_ts", "nickname": "nickname", "liked_count": "liked",
            "collected_count": "collected", "comment_count": "comment_count",
            "share_count": "shared", "note_url": "url", "source_keyword": "source_keyword",
            "image_list": "images", "video_url": "video", "tag_list": "tags",
        },
        "comment": {
            "comment_id": "comment_id", "create_time": "comment_ts", "content": "content",
            "nickname": "nickname", "like_count": "like_count", "sub_comment_count": "sub_count",
            "parent_comment_id": "parent_id", "note_id": "ref_id", "小红书号": "red_id",
            "pictures": "pictures",
        },
    },
    "dy": {
        "name": "抖音",
        "content": {
            "aweme_id": "id", "aweme_type": "item_type", "title": "title", "desc": "desc",
            "create_time": "pub_ts", "nickname": "nickname", "liked_count": "liked",
            "collected_count": "collected", "comment_count": "comment_count",
            "share_count": "shared", "aweme_url": "url", "source_keyword": "source_keyword",
            "cover_url": "cover", "video_download_url": "video", "note_download_url": "images",
        },
        "comment": {
            "comment_id": "comment_id", "create_time": "comment_ts", "content": "content",
            "nickname": "nickname", "like_count": "like_count", "sub_comment_count": "sub_count",
            "parent_comment_id": "parent_id", "aweme_id": "ref_id", "pictures": "pictures",
        },
    },
    "wb": {
        "name": "微博",
        "content": {
            "note_id": "id", "content": "desc", "create_time": "pub_ts",
            "nickname": "nickname", "liked_count": "liked", "comments_count": "comment_count",
            "shared_count": "shared", "note_url": "url", "source_keyword": "source_keyword",
        },
        "comment": {
            "comment_id": "comment_id", "create_time": "comment_ts", "content": "content",
            "nickname": "nickname", "comment_like_count": "like_count",
            "sub_comment_count": "sub_count", "parent_comment_id": "parent_id",
            "note_id": "ref_id",
        },
    },
    # 字段名以真实 jsonl 样本确认（data/toutiao/jsonl/，2026-11-10）。
    # 头条内容不含点赞/收藏/评论/转发计数字段，Excel 对应列自然缺省，属平台数据限制。
    "toutiao": {
        "name": "今日头条",
        "content": {
            "note_id": "id", "type": "item_type", "title": "title", "desc": "desc",
            "time": "pub_ts", "nickname": "nickname", "note_url": "url",
            "source_keyword": "source_keyword",
        },
        "comment": {
            "comment_id": "comment_id", "create_time": "comment_ts", "content": "content",
            "nickname": "nickname", "like_count": "like_count",
            "sub_comment_count": "sub_count", "parent_comment_id": "parent_id",
            "note_id": "ref_id", "note_url": "url",
        },
    },
}

# ---------------- 标准字段 -> 中文列 ----------------
CONTENT_CN = {
    "id": "帖子ID", "item_type": "类型", "title": "标题", "desc": "正文",
    "pub_time": "发布时间", "nickname": "作者", "liked": "点赞数",
    "collected": "收藏数", "comment_count": "评论数", "shared": "转发数",
    "url": "原帖链接", "source_keyword": "来源关键词", "images": "图片",
    "video": "视频", "cover": "封面", "tags": "标签",
}
COMMENT_CN = {
    "comment_id": "评论ID", "comment_time": "评论时间", "content": "评论内容",
    "nickname": "作者", "like_count": "点赞数", "sub_count": "子评论数",
    "parent_id": "父评论ID", "ref_id": "关联帖子ID", "red_id": "小红书号",
    "pictures": "图片", "url": "原帖链接",
}


def _task_keyword_set(task: Dict) -> Optional[Set[str]]:
    """解析任务关键词为集合（兼容中英文逗号、空格）。非关键词任务返回 None，不做过滤。"""
    if task.get("input_type") != task_store.INPUT_KEYWORD:
        return None
    parts = re.split(r"[,，]", str(task.get("keywords", "") or ""))
    kws = {p.strip() for p in parts if p.strip()}
    return kws or None


def _read_all_rows(platform: str, kind: str) -> List[Dict]:
    """读取某平台 jsonl 下全部 contents/comments，按主键去重合并。"""
    base = os.path.join(os.getcwd(), "data", data_dir_of(platform), "jsonl")
    pattern = "*_contents_*.jsonl" if kind == "content" else "*_comments_*.jsonl"
    pk = "note_id" if kind == "content" else "comment_id"
    merged: Dict[str, Dict] = {}
    for f in glob.glob(os.path.join(base, pattern)):
        try:
            import json
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    # 抖音内容主键是 aweme_id
                    key = row.get("aweme_id") if platform == "dy" and kind == "content" else row.get(pk)
                    if key is None:
                        continue
                    merged[str(key)] = row
        except OSError:
            pass
    return list(merged.values())


def _fmt_ts(v) -> str:
    try:
        n = float(v)
        if n > 1e12:
            n /= 1000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n))
    except Exception:
        return ""


def _to_standard(rows: List[Dict], mapping: Dict[str, str]) -> List[Dict]:
    out = []
    for row in rows:
        std = {}
        for raw_key, std_key in mapping.items():
            if raw_key in row:
                std[std_key] = row[raw_key]
        out.append(std)
    return out


def _build_content_df(platform: str, win_lo: Optional[int], win_hi: Optional[int],
                      allowed_keywords: Optional[Set[str]] = None) -> pd.DataFrame:
    meta = PLATFORM_META[platform]
    rows = _read_all_rows(platform, "content")
    std = _to_standard(rows, meta["content"])
    # 时间窗过滤
    filtered = []
    for r in std:
        # 任务关键词过滤：只保留本任务关键词采到的帖子，避免历史其他关键词数据混入
        if allowed_keywords is not None:
            sk = str(r.get("source_keyword", "") or "").strip()
            if sk not in allowed_keywords:
                continue
        ts = r.get("pub_ts")
        try:
            t = time_util.to_seconds(ts)
        except (TypeError, ValueError):
            filtered.append(r)
            continue
        if win_lo is not None and t < win_lo:
            continue
        if win_hi is not None and t > win_hi:
            continue
        r["pub_time"] = _fmt_ts(ts)
        filtered.append(r)
    df = pd.DataFrame(filtered)
    if not df.empty and "pub_ts" in df.columns:
        df = df.sort_values("pub_ts", ascending=False)
        df = df.drop(columns=["pub_ts"])
    return df


def _build_comment_df(platform: str, win_lo: Optional[int], win_hi: Optional[int],
                      content_url_map: Dict[str, str]) -> pd.DataFrame:
    meta = PLATFORM_META[platform]
    rows = _read_all_rows(platform, "comment")
    std = _to_standard(rows, meta["comment"])
    filtered = []
    for r in std:
        ts = r.get("comment_ts")
        try:
            t = time_util.to_seconds(ts)
        except (TypeError, ValueError):
            filtered.append(r)
            continue
        if win_lo is not None and t < win_lo:
            continue
        if win_hi is not None and t > win_hi:
            continue
        r["comment_time"] = _fmt_ts(ts)
        # 关联原帖链接：优先用本任务采到的帖子链接；缺失时回退评论自带的链接（如头条）
        ref = str(r.get("ref_id", ""))
        r["url"] = content_url_map.get(ref) or r.get("url", "")
        filtered.append(r)
    df = pd.DataFrame(filtered)
    if not df.empty and "comment_ts" in df.columns:
        df = df.sort_values("comment_ts", ascending=False)
        df = df.drop(columns=["comment_ts"])
    return df


def _add_hyperlinks(ws, col_name: str) -> None:
    headers = [cell.value for cell in ws[1]]
    if col_name not in headers:
        return
    col = headers.index(col_name) + 1
    for r in range(2, ws.max_row + 1):
        cell = ws.cell(row=r, column=col)
        if cell.value:
            cell.hyperlink = cell.value
            cell.style = "Hyperlink"


def count_task_rows(task: Dict, platform: str,
                    win_start: Optional[str] = None,
                    win_end: Optional[str] = None) -> Tuple[int, int]:
    """统计本任务在某平台、窗口内、关键词下、去重后的总条数（帖子, 评论）。

    过滤口径与 export_task 完全一致，保证卡片上的总数和点「导出」看到的行数对得上。
    win_start/win_end 显式传入时优先使用（每日任务运行中用本轮真实窗口，
    避免读到创建时/上一轮的旧窗口）；缺省回退任务存储窗口。
    """
    meta = PLATFORM_META.get(platform)
    if meta is None:
        return (0, 0)
    ws = task.get("time_start", "") if win_start is None else win_start
    we = task.get("time_end", "") if win_end is None else win_end
    win_lo = time_util.iso_to_ts(str(ws or ""))
    win_hi = time_util.iso_to_ts(str(we or ""))
    allowed = _task_keyword_set(task)

    def in_window(ts) -> bool:
        try:
            t = time_util.to_seconds(ts)
        except (TypeError, ValueError):
            return True  # 无法解析时间的行保留，与导出处理一致
        if win_lo is not None and t < win_lo:
            return False
        if win_hi is not None and t > win_hi:
            return False
        return True

    # 帖子：关键词任务按 source_keyword 过滤，再按发布时间窗过滤
    content_std = _to_standard(_read_all_rows(platform, "content"), meta["content"])
    total_c = 0
    for r in content_std:
        if allowed is not None:
            sk = str(r.get("source_keyword", "") or "").strip()
            if sk not in allowed:
                continue
        if not in_window(r.get("pub_ts")):
            continue
        total_c += 1

    # 评论：评论不按关键词过滤（与导出一致），仅按评论时间窗过滤
    comment_std = _to_standard(_read_all_rows(platform, "comment"), meta["comment"])
    total_m = sum(1 for r in comment_std if in_window(r.get("comment_ts")))
    return (total_c, total_m)


def export_task(task_id: str, win_start: str, win_end: str) -> str:
    """导出一个任务的多 sheet Excel，返回文件绝对路径。"""
    task = task_store.get_task(task_id)
    if task is None:
        raise KeyError(f"任务不存在：{task_id}")
    win_lo = time_util.iso_to_ts(win_start)
    win_hi = time_util.iso_to_ts(win_end)
    kind = task["data_mode"]
    allowed_keywords = _task_keyword_set(task)

    # 评论模式需要帖子链接映射（用于关联原帖）
    content_url_by_platform: Dict[str, Dict[str, str]] = {}
    for p in task["platforms"]:
        if p not in PLATFORM_META:
            continue  # 未适配平台不参与链接映射，也不阻断导出
        content_rows = _read_all_rows(p, "content")
        m = PLATFORM_META[p]
        url_map = {}
        for row in content_rows:
            std = _to_standard([row], m["content"])
            if std:
                s = std[0]
                if s.get("id") and s.get("url"):
                    url_map[str(s["id"])] = s["url"]
        content_url_by_platform[p] = url_map

    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = task["name"]
    xlsx = os.path.join(os.getcwd(), "data", f"{safe_name}_{ts}.xlsx")
    seq = 1
    while os.path.exists(xlsx):
        xlsx = os.path.join(os.getcwd(), "data", f"{safe_name}_{ts}_{seq:02d}.xlsx")
        seq += 1

    cn_map = CONTENT_CN if kind == "content" else COMMENT_CN
    with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
        for platform in task["platforms"]:
            if platform not in PLATFORM_META:
                # 未适配平台：写提示 sheet，不阻断其余平台导出（与「单平台失效不停整个任务」一致）
                note = pd.DataFrame({"说明": [f"平台 {platform} 字段映射尚未适配，数据未纳入导出"]})
                note.to_excel(writer, sheet_name=platform, index=False)
                continue
            if kind == "content":
                df = _build_content_df(platform, win_lo, win_hi, allowed_keywords)
                link_col = "原帖链接"
            else:
                df = _build_comment_df(platform, win_lo, win_hi,
                                       content_url_by_platform.get(platform, {}))
                link_col = "原帖链接"
            if not df.empty:
                # 只保留已知中文列，按映射顺序排列
                df = df.rename(columns=cn_map)
                ordered = [c for c in cn_map.values() if c in df.columns]
                extras = [c for c in df.columns if c not in ordered]
                df = df[ordered + extras]
            sheet = PLATFORM_META[platform]["name"]
            if df.empty:
                df = pd.DataFrame({(cn_map.get("desc") or "数据"): []})
            df.to_excel(writer, sheet_name=sheet, index=False)
            _add_hyperlinks(writer.sheets[sheet], link_col)

    return xlsx
