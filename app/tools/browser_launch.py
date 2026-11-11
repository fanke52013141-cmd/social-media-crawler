# -*- coding: utf-8 -*-
# 统一的静默浏览器启动策略。
# 背景：chrome-headless-shell 会被小红书、抖音等平台识别为自动化而触发风控重定向，
# 即使带着有效登录 cookie 也无法正常进入。重点平台改用「完整 Chrome + 离屏窗口」：
# 窗口移到屏幕外，用户视觉上看不到，但浏览器是完整 Chrome，可规避无头检测。
OFFSCREEN_PLATFORMS = ("xhs", "dy", "wb")
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
    """重点平台返回离屏窗口参数，其他平台返回空列表。"""
    if platform in OFFSCREEN_PLATFORMS:
        return list(_OFFSCREEN_ARGS)
    return []


# Patchright 隐身驱动的平台：用系统真实 Edge，而非自带 Chromium。
# 实测驱动真实浏览器对规避自动化检测的作用，甚至高于 CDP 补丁本身。
PATCHRIGHT_PLATFORMS = ("xhs", "dy", "wb")


def browser_channel(platform: str):
    """Patchright 平台返回系统真实浏览器通道（msedge），其余返回 None 走默认浏览器。"""
    if platform in PATCHRIGHT_PLATFORMS:
        return "msedge"
    return None
