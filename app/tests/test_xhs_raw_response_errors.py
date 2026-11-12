# -*- coding: utf-8 -*-

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from tenacity import RetryError

from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.exception import (
    IPBlockError,
    PlatformAccessError,
    RiskControlError,
)


class FakeAsyncClient:
    def __init__(self, request_impl):
        self.request_impl = request_impl

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def request(self, *args, **kwargs):
        return await self.request_impl(*args, **kwargs)


def make_client():
    client = XiaoHongShuClient(
        headers={"Cookie": "web_session=test"},
        playwright_page=object(),
        cookie_dict={},
    )
    client._refresh_proxy_if_expired = AsyncMock()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_raw_response_rejects_access_http_status(monkeypatch, status_code):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            text="blocked",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(PlatformAccessError):
        await make_client().request(
            "GET", "https://www.xiaohongshu.com/user/profile/test", return_response=True
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_raw_response_429_circuit_breaks(monkeypatch):
    """HTTP 429：平台已限流 → 立即熔断（RiskControlError，不重试），
    交编排器阶梯冷却。风控窗口内硬重试会把限制升级为警告/封禁。"""
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            text="rate limited",
            headers={"Retry-After": "1"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(RiskControlError):
        await make_client().request(
            "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
        )

    assert calls == 1  # 首次信号即收手，不做秒级硬重试


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_exception", "return_response"),
    [
        (300012, IPBlockError, True),
        ("300012", IPBlockError, False),
    ],
)
async def test_raw_response_rejects_known_business_block(
    monkeypatch, code, expected_exception, return_response
):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": False, "code": code, "msg": "blocked"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(expected_exception):
        await make_client().request(
            "GET",
            "https://www.xiaohongshu.com/explore/test",
            return_response=return_response,
        )

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [300011, "300011"])
async def test_raw_response_security_limit_circuit_breaks(monkeypatch, code):
    """账号安全限制(300011)是警告前兆 → 立即熔断（不重试），交编排器阶梯冷却。"""
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": False, "code": code, "msg": "security limit"},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(RiskControlError):
        await make_client().request(
            "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_raw_response_keeps_successful_html(monkeypatch):
    async def request_impl(method, url, **kwargs):
        return httpx.Response(
            200,
            text="<html>ok</html>",
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    result = await make_client().request(
        "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
    )

    assert result == "<html>ok</html>"


@pytest.mark.asyncio
async def test_successful_json_keeps_raw_and_parsed_return_modes(monkeypatch):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"success": True, "data": {"id": "test"}},
            request=httpx.Request(method, url),
        )

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )
    client = make_client()

    raw_result = await client.request(
        "GET", "https://www.xiaohongshu.com/explore/test", return_response=True
    )
    parsed_result = await client.request(
        "GET", "https://edith.xiaohongshu.com/api/test"
    )

    assert json.loads(raw_result) == {"success": True, "data": {"id": "test"}}
    assert parsed_result == {"id": "test"}
    assert calls == 2


@pytest.mark.asyncio
async def test_html_detail_does_not_multiply_transport_retries(monkeypatch):
    calls = 0

    async def request_impl(method, url, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=httpx.Request(method, url))

    monkeypatch.setattr(
        "media_platform.xhs.client.make_async_client",
        lambda **kwargs: FakeAsyncClient(request_impl),
    )

    with pytest.raises(RetryError):
        await make_client().get_note_by_id_from_html(
            "test", xsec_source="pc_search", xsec_token="token"
        )

    assert calls == 3


@pytest.mark.asyncio
async def test_html_detail_still_retries_parse_failures():
    client = make_client()
    client.request = AsyncMock(return_value="<html>incomplete</html>")
    client._extractor.extract_note_detail_from_html = Mock(
        side_effect=ValueError("incomplete initial state")
    )

    with pytest.raises(RetryError):
        await client.get_note_by_id_from_html(
            "test", xsec_source="pc_search", xsec_token="token"
        )

    assert client.request.await_count == 3
    assert client._extractor.extract_note_detail_from_html.call_count == 3
