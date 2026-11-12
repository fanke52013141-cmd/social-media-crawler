# -*- coding: utf-8 -*-
# 登录态体检工具：静默启动各平台持久 profile，读取登录 cookie 判断是否仍有效，
# 结果写入 data/login_state.json，供任务创建前展示「有效/失效」。
import json
import os
import tempfile
import time
from typing import Dict, List, Optional

from playwright.async_api import async_playwright

STATE_FILE = os.path.join(os.getcwd(), "data", "login_state.json")

# 各平台登录态判定信息：
# url=用于激活该域 cookie 的页面；cookie_keys=登录凭证（任一存在即视为已登录）；
# profile=browser_data 下的登录目录名；local_storage=需额外检查的本地存储标记。
PLATFORM_LOGIN_INFO: Dict[str, Dict] = {
    "xhs": {
        "name": "小红书",
        "url": "https://www.xiaohongshu.com",
        "cookie_keys": ["web_session"],
        "profile": "xhs_user_data_dir",
        "local_storage": None,
    },
    "dy": {
        "name": "抖音",
        "url": "https://www.douyin.com",
        "cookie_keys": ["LOGIN_STATUS", "sessionid", "sid_tt"],
        "profile": "dy_user_data_dir",
        "local_storage": "HasUserLogin",
    },
    "wb": {
        "name": "微博",
        "url": "https://weibo.com",
        "cookie_keys": ["SSOLoginState", "SUB"],
        "profile": "wb_user_data_dir",
        "local_storage": None,
    },
    "toutiao": {
        "name": "今日头条",
        "url": "https://www.toutiao.com",
        "cookie_keys": ["sessionid", "sid_tt", "uid_tt", "sessionid_ss"],
        "profile": "toutiao_user_data_dir",
        "local_storage": None,
    },
    "ks": {
        "name": "快手",
        "url": "https://www.kuaishou.com",
        "cookie_keys": ["passToken", "userId"],
        "profile": "ks_user_data_dir",
        "local_storage": None,
    },
    "bili": {
        "name": "B站",
        "url": "https://www.bilibili.com",
        "cookie_keys": ["SESSDATA", "DedeUserID"],
        "profile": "bili_user_data_dir",
        "local_storage": None,
    },
    "tieba": {
        "name": "贴吧",
        "url": "https://tieba.baidu.com",
        "cookie_keys": ["BDUSS", "STOKEN"],
        "profile": "tieba_user_data_dir",
        "local_storage": None,
    },
    "zhihu": {
        "name": "知乎",
        "url": "https://www.zhihu.com",
        "cookie_keys": ["z_c0", "d_c0"],
        "profile": "zhihu_user_data_dir",
        "local_storage": None,
    },
}

# 平台固定展示顺序（与工作台一致，前三为重点）
PLATFORM_ORDER = ["xhs", "dy", "wb", "toutiao", "ks", "bili", "tieba", "zhihu"]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _profile_dir(platform: str) -> str:
    info = PLATFORM_LOGIN_INFO[platform]
    return os.path.join(os.getcwd(), "browser_data", info["profile"])


def read_state() -> Dict[str, Dict]:
    """读取缓存的体检结果（不启动浏览器）。"""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(data: Dict[str, Dict]) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def snapshot() -> List[Dict]:
    """给前端的轻量快照：含 profile 是否存在与缓存的登录结论，不启动浏览器。"""
    cached = read_state()
    out = []
    for code in PLATFORM_ORDER:
        info = PLATFORM_LOGIN_INFO[code]
        profile_exists = os.path.isdir(_profile_dir(code))
        item = cached.get(code, {})
        out.append({
            "code": code,
            "name": info["name"],
            "profile_exists": profile_exists,
            "logged_in": bool(item.get("logged_in")) if profile_exists else False,
            "last_checked": item.get("last_checked", ""),
            "detail": item.get("detail", ""),
        })
    return out


async def _xhs_server_verify(context, page) -> Optional[bool]:
    """小红书服务端二次确认：用 xhshow 签名实调 selfinfo，按业务码判断是否真实登录。

    游客访问也会被分配临时 web_session，仅看 cookie 会把游客误判为登录；
    这里与采集进程 pong 用同一标准（success=true 且 code=0）。
    返回 True=已登录，False=未登录/游客，None=校验流程异常（调用方回退）。
    """
    try:
        import httpx
        from media_platform.xhs.playwright_sign import sign_with_xhshow

        cookies = await context.cookies()
        cookie_str = "; ".join(f"{c.get('name')}={c.get('value')}" for c in cookies)
        if "web_session=" not in cookie_str:
            return False
        uri = "/api/sns/web/v1/user/selfinfo"
        signs = sign_with_xhshow(uri=uri, data={}, cookie_str=cookie_str, method="GET")
        ua = await page.evaluate("() => navigator.userAgent")
        headers = {
            "user-agent": ua,
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://www.xiaohongshu.com",
            "referer": "https://www.xiaohongshu.com/",
            "Cookie": cookie_str,
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
        }
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://edith.xiaohongshu.com" + uri, headers=headers)
            if r.status_code != 200:
                return None
            body = r.json()
            return bool(body.get("success")) and body.get("code") == 0
    except Exception:
        return None


async def check_platform(platform: str, headless: bool = False) -> Dict:
    """启动浏览器实际检测单个平台登录态，并更新状态文件。

    默认有头离屏：完整 Chrome 窗口移到屏幕外（视觉静默），规避 headless-shell
    被小红书等平台识别为自动化而触发风控重定向的问题。
    """
    if platform not in PLATFORM_LOGIN_INFO:
        raise ValueError(f"unsupported platform: {platform}")
    info = PLATFORM_LOGIN_INFO[platform]
    profile = _profile_dir(platform)

    result = {
        "logged_in": False,
        "profile_exists": os.path.isdir(profile),
        "last_checked": _now(),
        "detail": "",
    }

    if not result["profile_exists"]:
        result["detail"] = "未登录"
        _merge(platform, result)
        return result

    launch_args = ["--disable-blink-features=AutomationControlled"]
    if not headless:
        # 离屏有头：窗口移出可见区域，用户看不到但浏览器是完整 Chrome；
        # 「显示浏览器窗口」打开时不做离屏，窗口可见。
        from tools import settings
        if not settings.browser_visible():
            launch_args += ["--window-position=-32000,-32000", "--window-size=1440,900"]

    last_err: Optional[Exception] = None
    for _attempt in range(2):
        try:
            async with async_playwright() as p:
                context = await p.chromium.launch_persistent_context(
                    user_data_dir=profile,
                    headless=headless,
                    accept_downloads=True,
                    args=launch_args,
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(info["url"], wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    cookies = await context.cookies()
                    cookie_dict = {c.get("name"): c.get("value") for c in cookies}
                    hit_key = next((k for k in info["cookie_keys"] if cookie_dict.get(k)), None)

                    storage_ok = False
                    if info["local_storage"]:
                        try:
                            ls = await page.evaluate("() => window.localStorage")
                            storage_ok = ls.get(info["local_storage"], "") == "1"
                        except Exception:
                            storage_ok = False

                    if hit_key or storage_ok:
                        if platform == "xhs":
                            # 游客也有临时 web_session，需服务端业务码二次确认
                            verified = await _xhs_server_verify(context, page)
                            if verified is False:
                                result["logged_in"] = False
                                result["detail"] = "登录已失效（当前为游客临时会话），请重新扫码"
                            elif verified is True:
                                result["logged_in"] = True
                                result["detail"] = "登录有效"
                            else:
                                # 校验流程异常，回退 cookie 判断，避免误拦
                                result["logged_in"] = True
                                result["detail"] = "登录有效（服务端校验未完成，按缓存结论）"
                        else:
                            result["logged_in"] = True
                            result["detail"] = "登录有效"
                    else:
                        result["logged_in"] = False
                        result["detail"] = "登录已失效，请重新扫码"
                finally:
                    await context.close()
            last_err = None
            break
        except Exception as e:
            last_err = e

    if last_err is not None:
        # 体检异常（网络抖动/导航中断等）≠ 登录失效：沿用上次缓存结论，
        # 避免把健康平台误判为失效而跳过整个平台的采集。
        cached = read_state().get(platform, {})
        result["logged_in"] = bool(cached.get("logged_in"))
        result["detail"] = f"体检异常（沿用上次结论）：{last_err}"

    _merge(platform, result)
    return result


def _merge(platform: str, item: Dict) -> None:
    data = read_state()
    data[platform] = item
    _write_state(data)


def mark_logged_in(platform: str, detail: str = "登录有效") -> Dict:
    """登录流程已确认成功后，就地回写缓存结论（不启动浏览器）。

    采集进程内已持有打开的 profile，再启动体检进程会与同一 profile 冲突，
    因此登录成功后由采集流程直接更新状态文件，保证前端圆点即时变绿、
    页面刷新后结论仍正确。
    """
    if platform not in PLATFORM_LOGIN_INFO:
        raise ValueError(f"unsupported platform: {platform}")
    result = {
        "logged_in": True,
        "profile_exists": os.path.isdir(_profile_dir(platform)),
        "last_checked": _now(),
        "detail": detail,
    }
    _merge(platform, result)
    return result


async def check_all(headless: bool = False) -> List[Dict]:
    """检测所有平台。"""
    for code in PLATFORM_ORDER:
        await check_platform(code, headless=headless)
    return snapshot()


if __name__ == "__main__":
    import asyncio
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if target == "all":
        asyncio.run(check_all())
    elif target:
        asyncio.run(check_platform(target))
    else:
        print(json.dumps(snapshot(), ensure_ascii=False, indent=2))
