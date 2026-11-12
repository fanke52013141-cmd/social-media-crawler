# -*- coding: utf-8 -*-
# 统一的静默浏览器启动策略。
# 背景：chrome-headless-shell 会被小红书、抖音等平台识别为自动化而触发风控重定向，
# 即使带着有效登录 cookie 也无法正常进入。重点平台改用「完整 Chrome + 离屏窗口」：
# 窗口移到屏幕外，用户视觉上看不到，但浏览器是完整 Chrome，可规避无头检测。
# 工作台「显示浏览器窗口」开关打开时，窗口正常显示在屏幕上（便于用户观察/排查）。
from tools import settings

OFFSCREEN_PLATFORMS = ("xhs", "dy", "wb", "toutiao", "ks", "bili", "tieba", "zhihu")
# 离屏窗口 + 反自动化启动参数（三平台统一入口，只改这里即全部生效）。
# 这些参数在浏览器进程层抹掉自动化痕迹，与 stealth.min.js（页面层）形成双保险。
_OFFSCREEN_ARGS = [
    "--window-position=-32000,-32000",
    "--window-size=1920,1080",
    "--disable-blink-features=AutomationControlled",  # 移除 navigator.webdriver 自动化标记
    "--no-first-run",                                 # 跳过首次运行引导
    "--no-default-browser-check",                     # 跳过默认浏览器检查弹窗
    "--disable-infobars",                             # 禁用“正被自动测试软件控制”提示栏
    "--lang=zh-CN",                                   # 语言与中文环境保持一致
]


def effective_headless(platform: str, configured_headless: bool) -> bool:
    """实际传给 Playwright 的 headless：重点平台强制 False（有头）。"""
    if platform in OFFSCREEN_PLATFORMS:
        return False
    return configured_headless


def offscreen_args(platform: str) -> list:
    """重点平台返回离屏窗口参数，其他平台返回空列表。

    「显示浏览器窗口」打开时所有平台都返回空列表——窗口留在屏幕内可见。
    """
    if settings.browser_visible():
        return []
    if platform in OFFSCREEN_PLATFORMS:
        return list(_OFFSCREEN_ARGS)
    return []


# Patchright 隐身驱动的平台：用系统真实 Edge，而非自带 Chromium。
# 实测驱动真实浏览器对规避自动化检测的作用，甚至高于 CDP 补丁本身。
PATCHRIGHT_PLATFORMS = ("xhs", "dy", "wb", "toutiao", "ks", "bili", "tieba", "zhihu")


def browser_channel(platform: str):
    """Patchright 平台返回系统真实浏览器通道，其余返回 None 走默认浏览器。
    自动探测已安装的浏览器：优先 Edge，其次 Chrome；都没有则回退自带 Chromium，
    避免硬编码 msedge 在未装 Edge 的机器上直接启动失败。"""
    if platform not in PATCHRIGHT_PLATFORMS:
        return None
    import os
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    edge_paths = [
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    chrome_paths = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    if any(os.path.exists(p) for p in edge_paths):
        return "msedge"
    if any(os.path.exists(p) for p in chrome_paths):
        return "chrome"
    return None
