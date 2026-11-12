# -*- coding: utf-8 -*-
# 浏览器 TLS 指纹请求通道（curl_cffi）。
# 背景：httpx 的 TLS 握手指纹是 Python 的，与驱动页面的真实浏览器不一致，
# 平台可据此识别「签名正确但请求方不是浏览器」（风控强信号）。curl_cffi 模拟
# 真实浏览器的 TLS/JA3 指纹，社区项目（如 CreatorHub）用同一方案降低识别风险。
# 接口与 httpx.AsyncClient 对齐（request/status_code/headers/json/text），
# 仅替换传输层，业务代码无感。
from curl_cffi.requests import AsyncSession

import config

# curl_cffi 内置的真实浏览器指纹目标（跟随其版本更新的 Chrome 指纹）
IMPERSONATE = "chrome"


def make_tls_client(**kwargs) -> AsyncSession:
    """创建带浏览器 TLS 指纹的异步请求会话。

    参数与 httpx.AsyncClient 常用项兼容：proxy（URL 串）、timeout、headers 等。
    verify 默认开启 SSL 校验，仅企业中间人代理场景按配置关闭。
    """
    kwargs.setdefault("impersonate", IMPERSONATE)
    kwargs.setdefault("verify", not getattr(config, "DISABLE_SSL_VERIFY", False))
    return AsyncSession(**kwargs)