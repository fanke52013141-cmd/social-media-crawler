# -*- coding: utf-8 -*-
"""把技术运行日志翻译成用户能看懂的结构化进度。

设计原则：
1. 集中维护「日志模式 → 友好进度」规则，三平台 core 不做任何埋点，降低侵入；
2. 输出固定顺序的里程碑（打开平台 → 登录 → 搜索/定位 → 采集帖子 → 采集评论 → 完成），
   采集类动作按次数累计，原始技术日志仍由 /api/status 单独返回、前端折叠展示；
3. 规则匹配不到的日志不影响主流程，保证健壮。
"""
import re
from typing import Dict, List, Optional

# 里程碑 key（顺序即时间线顺序）
M_LAUNCH = "launch"
M_LOGIN = "login"
M_SEARCH = "search"
M_COLLECT = "collect"
M_COMMENT = "comment"
M_DONE = "done"

MILESTONE_LABEL = {
    M_LAUNCH: "打开平台",
    M_LOGIN: "登录确认",
    M_SEARCH: "搜索定位",
    M_COLLECT: "采集帖子",
    M_COMMENT: "采集评论",
    M_DONE: "完成",
}

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})")
_KW_RE = re.compile(r"Current search keyword:\s*(.+?)\s*$")
_TIME_RE = re.compile(r"(\d{2}:\d{2}:\d{2})")
# 详情查看按 note_id 去重：同一笔记被多个关键词重复查看只算一篇，
# 保证「已查看 = 已采集 + 已过滤 + 未入库」漏斗等式成立。
_DETAIL_ID_RE = re.compile(r"Begin get note detail, note_id:\s*(\S+)")
# 详情获取失败（平台拒绝/笔记已删除/风控拦截），core.py 各异常分支的日志特征
_FAIL_ID_RE = re.compile(
    r"(?:\[skip\] Failed to get note detail, Id:|Note not found:"
    r"|Access restricted while getting note|have not fund note detail note_id:)\s*(\S+)")

# 单条日志规则：(正则, 里程碑, 友好文案模板, 计数类型)
# 计数类型：None / "saved" / "skip" / "comment"
RULES = [
    (re.compile(r"Launching browser"), M_LAUNCH, "正在打开{pname}", None),
    (re.compile(r"Guest mode enabled", re.I), M_LOGIN, "游客模式，无需登录", None),
    (re.compile(r"Begin pong", re.I), M_LOGIN, "正在确认登录状态", None),
    (re.compile(r"cookie may be invalid|again login|not logged|login wall", re.I),
     M_LOGIN, "登录已失效，准备扫码", None),
    (re.compile(r"Begin login|login_by_qrcode|请使用.*扫码|等待中", re.I),
     M_LOGIN, "等待扫码登录", None),
    (re.compile(r"Login successful", re.I), M_LOGIN, "扫码成功，正在继续", None),
    (re.compile(r"Current search keyword|Begin search|search.*keyword|search weibo keyword", re.I),
     M_SEARCH, None, None),
    (re.compile(r"Fetching full text|Begin get note detail|get_note_info|get_aweme_detail|Fetching note", re.I),
     M_COLLECT, "正在查看帖子", None),
    (re.compile(r"store\.(xhs|douyin|weibo)\..*update_.*(note|aweme)"),
     M_COLLECT, None, "saved"),
    (re.compile(r"skip out-of-window"), M_COLLECT, None, "skip"),
    (re.compile(r"\[skip\] Failed to get note detail|Note not found:"
                r"|Access restricted while getting note|have not fund note detail"),
     M_COLLECT, "个别帖子获取失败，已跳过继续", None),
    (re.compile(r"get_note_comments|get_comments|batch get note comments|note comment", re.I),
     M_COMMENT, None, "comment"),
    (re.compile(r"humanized pause|human pause", re.I), None, "正在模拟真人浏览，稍作停留", None),
    (re.compile(r"Crawler finished|finished \.\.\.", re.I), M_DONE, "本次采集完成", None),
]

# 高危/异常信号（用于友好错误提示）
ERROR_RE = re.compile(
    r"captcha|security limit|been blocked|Rate limit|status code[=: ]+(429|461|471)|风控|封禁|滑块",
    re.I,
)
# 落盘日志里的标题（微博/抖音可提取，小红书是整段 dict，不提取）
TITLE_RE = re.compile(r"(?:note id|aweme id):\s*\d+\s*,\s*title:\s*(.+?)\s*\.{0,3}$")


def _line_time(line: str) -> str:
    m = _TS_RE.match(line)
    return m.group(2) if m else ""


def _saved_title(line: str) -> Optional[str]:
    m = TITLE_RE.search(line)
    if m:
        return m.group(1).strip()[:30]
    return None


def interpret(lines: List[str], pname: str, data_mode: str = "content",
              crawl_mode: str = "login", is_login_only: bool = False) -> Dict:
    """扫描日志，返回结构化进度。

    返回字段：
      timeline: 里程碑列表 [{key,label,text,time,state,counts}]，state=pending/active/done
      current:  当前友好文案（最新一条用户能看懂的话）
      counts:   {saved, skip, comment, viewed, failed}
                viewed 按 note_id 去重；failed 为详情获取失败的笔记数；
                未入库（前端计算）= viewed - saved - skip，其中含 failed 与处理中。
      finished: 是否完成
      error:    友好错误文案（无则空串）
    """
    order = [M_LAUNCH, M_LOGIN, M_SEARCH, M_COLLECT, M_COMMENT, M_DONE]
    if data_mode != "comment":
        order = [k for k in order if k != M_COMMENT]
    if is_login_only:
        # 仅登录模式：只到登录里程碑，随后即完成
        order = [M_LAUNCH, M_LOGIN, M_DONE]

    ms = {k: {"key": k, "label": MILESTONE_LABEL[k], "text": "",
              "time": "", "state": "pending", "counts": {}} for k in order}
    counts = {"saved": 0, "skip": 0, "comment": 0, "viewed": 0, "failed": 0}
    viewed_ids = set()
    viewed_anon = 0
    failed_ids = set()
    current = "准备开始"
    error = ""
    last_items: List[str] = []

    prev_key = None
    for line in lines:
        if not line.strip():
            continue
        # 详情查看进度：抓详情阶段即累计（按笔记去重），让用户在落盘前也能看到进展
        if "Begin get note detail" in line:
            dm = _DETAIL_ID_RE.search(line)
            if dm:
                viewed_ids.add(dm.group(1))
            else:
                viewed_anon += 1
        fm = _FAIL_ID_RE.search(line)
        if fm:
            failed_ids.add(fm.group(1))
        if ERROR_RE.search(line):
            error = "检测到平台安全验证或限流，已自动暂停以保护账号"
        matched = False
        for rx, mkey, tmpl, ctype in RULES:
            if not rx.search(line):
                continue
            matched = True
            t = _line_time(line)
            # 可计数事件
            if ctype:
                # 帖子模式下日志会出现 "comment mode is not enabled"，并非真的采到评论，不计数
                if ctype == "comment" and "not enabled" in line:
                    ctype = None
                else:
                    counts[ctype] += 1
                    if mkey and mkey in ms:
                        ms[mkey]["state"] = "active"
                        if not ms[mkey]["time"]:
                            ms[mkey]["time"] = t
                        if ctype == "saved":
                            title = _saved_title(line)
                            if title:
                                last_items.append(title)
                                last_items = last_items[-3:]
            # 阶段里程碑
            if mkey and mkey in ms:
                text = tmpl
                if text is None:
                    if mkey == M_SEARCH:
                        # 兼容两种日志：Current search keyword: X 与 search ... keyword: X, page: n
                        kwm = _KW_RE.search(line) or re.search(r"keyword:\s*([^,]+)", line)
                        if kwm:
                            text = "正在搜索「%s」" % kwm.group(1).strip()
                        elif ms[M_SEARCH]["text"]:
                            text = ms[M_SEARCH]["text"]  # 保留已提取的具体文案，不被通用行覆盖
                        else:
                            text = "正在搜索内容"
                    elif mkey == M_COMMENT:
                        text = "正在采集评论"
                    elif mkey == M_COLLECT:
                        text = "正在采集帖子"
                    else:
                        text = MILESTONE_LABEL[mkey]
                text = text.replace("{pname}", pname)
                ms[mkey]["text"] = text
                ms[mkey]["time"] = ms[mkey]["time"] or t
                ms[mkey]["state"] = "active"
                current = text
                # 上一个里程碑标记完成
                if prev_key and prev_key in ms and prev_key != mkey:
                    ms[prev_key]["state"] = "done"
                prev_key = mkey
            elif tmpl:
                current = tmpl.replace("{pname}", pname)
            break
        # 未匹配的技术日志忽略

    # 完成态收尾
    finished = ms[M_DONE]["state"] == "active" if M_DONE in ms else False
    if finished:
        for k in order:
            ms[k]["state"] = "done"
        current = ms[M_DONE]["text"] or "本次采集完成"
    elif prev_key and prev_key in ms:
        ms[prev_key]["state"] = "active"

    counts["viewed"] = len(viewed_ids) + viewed_anon
    counts["failed"] = len(failed_ids)

    # 采集里程碑挂计数
    if M_COLLECT in ms:
        ms[M_COLLECT]["counts"] = {"saved": counts["saved"], "skip": counts["skip"],
                                   "failed": counts["failed"]}
        if not ms[M_COLLECT]["text"] and counts["saved"]:
            ms[M_COLLECT]["text"] = "已采集 %d 条帖子" % counts["saved"]
    if M_COMMENT in ms:
        ms[M_COMMENT]["counts"] = {"comment": counts["comment"]}
        if counts["comment"] and not ms[M_COMMENT]["text"]:
            ms[M_COMMENT]["text"] = "已采集 %d 条评论" % counts["comment"]

    timeline = [ms[k] for k in order if ms[k]["state"] != "pending"]
    if not error and finished and counts["saved"] == 0 and data_mode != "comment" and not is_login_only:
        error = "没有找到符合时间范围的内容"

    return {
        "timeline": timeline,
        "current": current,
        "counts": counts,
        "finished": finished,
        "error": error,
        "last_items": last_items,
    }
