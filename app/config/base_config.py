# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py
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

# Basic configuration
PLATFORM = "xhs"  # Platform, xhs | dy | ks | bili | wb | tieba | zhihu

# 是否使用海外版小红书 (rednote.com)
# 开启后 API 走 webapi.rednote.com，cookie 域使用 .rednote.com
XHS_INTERNATIONAL = False

KEYWORDS = "编程副业,编程兼职"  # Keyword search configuration, separated by English commas
LOGIN_TYPE = "qrcode"  # qrcode or phone or cookie
# 抓取模式（所有平台通用，由工作台选择）：
# login=登录抓取：加载持久登录态、失效时扫码，可拿全量列表/深度分页/评论；
# guest=游客抓取：不加载账号、不扫码，使用独立的游客浏览器目录，仅采公开可见内容，零账号风险（批量/深度受限）。
CRAWL_MODE = "login"
COOKIES = ""
# 仅登录模式：完成登录（含扫码）后立即退出，不进入任何搜索/详情采集。
# 用于工作台「扫码登录」独立入口，登录路径与真实采集完全同源。默认 False，不影响原流程。
LOGIN_ONLY = False
CRAWLER_TYPE = (
    "search"  # Crawling type, search (keyword search) | detail (post details) | creator (creator homepage data)
)
# Whether to enable IP proxy
ENABLE_IP_PROXY = False

# Number of proxy IP pools
IP_PROXY_POOL_COUNT = 2

# Proxy IP provider name
IP_PROXY_PROVIDER_NAME = "kuaidaili"  # kuaidaili | wandouhttp | static

# Static proxy configuration (used when IP_PROXY_PROVIDER_NAME is set to "static")
# Format: "http://your_home_domain:port" or "http://user:password@your_home_domain:port"
STATIC_PROXY_URL = ""

# Setting to True will not open the browser (headless browser)
# Setting False will open a browser
# 已改为无头模式：浏览器在后台静默运行、不占屏幕；登录态失效时仅弹出一个小二维码窗口供扫码。
# 若遇到滑块等需人工处理的验证，可临时改回 False 用有头模式手动通过。
HEADLESS = True

# ==================== 时间窗口（帖子发布时间过滤） ====================
# 由定时任务传入，ISO 本地时间字符串，空串表示不限制。
# 同时作用于：① 采集时早于起始时间可提前停止翻页；② 导出时严格过滤，保证 Excel 只含窗口内数据。
TIME_START = ""
TIME_END = ""

# Whether to save login status
SAVE_LOGIN_STATE = True

# ==================== CDP (Chrome DevTools Protocol) 配置 ====================
# 是否启用 CDP 模式 - 使用用户本地的 Chrome/Edge 浏览器进行爬取，具有更好的反检测能力
# 开启后，会自动检测并启动用户的 Chrome/Edge 浏览器，通过 CDP 协议进行控制
# 该方式使用真实浏览器环境，包括用户的扩展、Cookie 和设置，大幅降低被风控检测的风险
ENABLE_CDP_MODE = False

# CDP 调试端口，用于与浏览器通信
# 如果端口被占用，系统会自动尝试下一个可用端口
CDP_DEBUG_PORT = 9222

# 自定义浏览器路径（可选）
# 如果为空，系统会自动检测 Chrome/Edge 的安装路径
# Windows 示例: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
# macOS 示例: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CUSTOM_BROWSER_PATH = ""

# 是否在 CDP 模式下启用无头模式
# 注意：即使设置为 True，某些反检测功能在无头模式下可能无法正常工作
CDP_HEADLESS = False

# 浏览器启动超时时间（秒）
BROWSER_LAUNCH_TIMEOUT = 60

# 是否连接用户已打开的浏览器，而不是启动新的浏览器
# 开启后，程序会连接一个已经启用了远程调试的浏览器
# 用户需要在 Chrome 中开启远程调试：chrome://inspect/#remote-debugging
# 或者使用命令行参数启动 Chrome：--remote-debugging-port=9222
# 这种方式反检测效果最好，因为直接使用用户真实浏览器的所有 Cookie、扩展和浏览历史
CDP_CONNECT_EXISTING = True

# 程序结束时是否自动关闭浏览器
# 设置为 False 可以保持浏览器运行，方便调试
AUTO_CLOSE_BROWSER = True

# Data saving type option configuration, supports: csv, db, json, jsonl, sqlite, excel, postgres. It is best to save to DB, with deduplication function.
SAVE_DATA_OPTION = "jsonl"  # csv or db or json or jsonl or sqlite or excel or postgres

# Data saving path, if not specified by default, it will be saved to the data folder.
SAVE_DATA_PATH = ""

# Browser file configuration cached by the user's browser
USER_DATA_DIR = "%s_user_data_dir"  # %s will be replaced by platform name

# The number of pages to start crawling starts from the first page by default
START_PAGE = 1

# Control the number of crawled videos/posts
CRAWLER_MAX_NOTES_COUNT = 15

# Controlling the number of concurrent crawlers
MAX_CONCURRENCY_NUM = 1

# 是否启用媒体下载（封面、视频，以及图文帖的图片），默认关闭。
# 开启后媒体文件按 {SAVE_DATA_PATH 或 data}/{platform}/media/{内容ID}/ 目录聚合存放。
# 支持的平台：xhs / dy / ks / bili / wb（tieba、zhihu 的数据结构中没有媒体字段，不支持）。
# 命令行开关：--get_media
ENABLE_GET_MEDIA = False

# Whether to enable comment crawling mode. Comment crawling is enabled by default.
ENABLE_GET_COMMENTS = True

# Control the number of crawled first-level comments (single video/post)
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 100000

# Whether to enable the mode of crawling second-level comments. By default, crawling of second-level comments is not enabled.
# If the old version of the project uses db, you need to refer to schema/tables.sql line 287 to add table fields.
ENABLE_GET_SUB_COMMENTS = True

# 是否解析评论人的小红书号（需逐个访问主页，是最敏感、最密集的请求）。
# 工作台提供开关：默认开启；关闭后不再访问任何主页，评论照常采集，只是“小红书号”留空，更安全更快。
# 命令行开关：--get_red_id
ENABLE_GET_RED_ID = True

# word cloud related
# Whether to enable generating comment word clouds
ENABLE_GET_WORDCLOUD = False
# Custom words and their groups
# Add rule: xx:yy where xx is a custom-added phrase, and yy is the group name to which the phrase xx is assigned.
CUSTOM_WORDS = {
    "零几": "年份",  # Recognize "zero points" as a whole
    "高频词": "专业术语",  # Example custom words
}

# Deactivate (disabled) word file path
STOP_WORDS_FILE = "./docs/hit_stopwords.txt"

# Chinese font file path
FONT_PATH = "./docs/STZHONGS.TTF"

# Crawl interval
CRAWLER_MAX_SLEEP_SEC = 6  # 兼容旧逻辑的固定间隔（已主要被下面的拟人化随机区间取代）

# ==================== 安全保守模式（默认开启） ====================
# 设计原则：不设任何数量上限、不关闭任何功能，仅通过“放慢节奏 + 拟人化行为 + 会话休息”
# 让采集行为更接近真人，降低被风控判定为异常浏览的概率。最终数据一条不少，只是过程更慢。
SAFE_MODE = True            # 安全保守模式总开关
HUMAN_BEHAVIOR = True       # 是否在页面上模拟真人鼠标移动、滚动、阅读停留

# 普通接口请求的随机间隔区间（秒），节奏较旧版放慢，模拟真人抖动
CRAWLER_SLEEP_MIN = 3
CRAWLER_SLEEP_MAX = 8

# 访问用户主页（解析小红书号）的随机间隔区间（秒），主页请求最敏感，大幅放慢
CREATOR_SLEEP_MIN = 15
CREATOR_SLEEP_MAX = 40

# 长尾停顿：每次等待都有一定概率变成长停顿，模拟真人走神/看内容（而非均匀机械节奏）
LONG_TAIL_PAUSE_PROB = 0.12     # 触发长停顿的概率
LONG_PAUSE_MIN = 20             # 长停顿时长区间（秒）
LONG_PAUSE_MAX = 90

# 会话制休息：每累计若干“动作”强制休息一次，歇完继续，不截断任务、不限制总量
SESSION_ACTION_COUNT = 8        # 每 N 个动作触发一次会话休息
SESSION_REST_MIN = 180         # 会话休息时长区间（秒，约 3-7 分钟）
SESSION_REST_MAX = 420

# 主页访问的额外长休息：每访问若干个新主页插一次长休息，之后继续、无人数上限
CREATOR_REST_EVERY = 6
CREATOR_LONG_REST_MIN = 120    # 主页长休息时长区间（秒，约 2-5 分钟）
CREATOR_LONG_REST_MAX = 300

# 小红书号缓存持久化文件（跨任务复用，避免重复访问同一个人的主页）
RED_ID_CACHE_FILE = "./data/red_id_cache.json"

# 是否禁用 SSL 证书验证。仅在使用企业代理、Burp Suite、mitmproxy 等会注入自签名证书的中间人代理时设为 True。
# 警告：禁用 SSL 验证将使所有流量暴露于中间人攻击风险，请勿在生产环境中开启。
DISABLE_SSL_VERIFY = False

from .bilibili_config import *
from .xhs_config import *
from .dy_config import *
from .ks_config import *
from .weibo_config import *
from .tieba_config import *
from .zhihu_config import *
from .toutiao_config import *
