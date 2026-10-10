
"""重试机制测试。

**B1 就是这样漏掉的**：call_with_retry 的 docstring 承诺重试网络抖动，
但网络层异常没有 HTTP 状态码，被一律当成"不可重试"直接抛出 ——
一次都没重试过。这一组测试锁住"哪些该重试、哪些不该"。

全部用桩注入，零网络调用。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from app.capabilities.base import HttpClient, call_with_retry
from app.config import Config
from app.core.errors import ApiError


def _counter(error_factory):
    """构造一个「每次调用都抛指定异常」的函数，并记录调用次数。"""
    calls: list[int] = []

    def fn():
        calls.append(1)
        raise error_factory()

    return fn, calls


class TestRetryDecision:
    """判定逻辑：哪些失败值得再试一次。"""

    def test_network_error_without_status_is_retried(self):
        """B1 回归。

        网络层异常（连接被拒 / DNS 失败 / 连接重置）没有 status_code，
        必须靠 retryable 语义位表达"值得再试"。修复前这里只调用 1 次。
        """
        fn, calls = _counter(lambda: ApiError("无法连接云端服务", retryable=True))
        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=3, delay=0)
        assert len(calls) == 3, f"应当重试 3 次，实际 {len(calls)} 次"

    def test_http_5xx_is_retried(self):
        fn, calls = _counter(lambda: ApiError("服务端错误", status_code=503))
        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=3, delay=0)
        assert len(calls) == 3

    def test_http_429_is_retried(self):
        fn, calls = _counter(lambda: ApiError("限流", status_code=429))
        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=2, delay=0)
        assert len(calls) == 2

    def test_http_401_is_not_retried(self):
        """密钥错误重试没有意义，只会让用户多等几秒。"""
        fn, calls = _counter(lambda: ApiError("密钥无效", status_code=401))
        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=3, delay=0)
        assert len(calls) == 1

    def test_http_400_is_not_retried(self):
        fn, calls = _counter(lambda: ApiError("参数错误", status_code=400))
        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=3, delay=0)
        assert len(calls) == 1

    def test_raw_connection_error_is_retried(self):
        """未包装成 ApiError 的裸网络异常也要重试。"""
        fn, calls = _counter(lambda: requests.ConnectionError("连接重置"))
        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=3, delay=0)
        assert len(calls) == 3

    def test_raw_value_error_is_not_retried(self):
        """非网络异常不该重试 —— 那是代码 bug，重试只会掩盖它。"""
        fn, calls = _counter(lambda: ValueError("代码写错了"))
        with pytest.raises(ValueError):
            call_with_retry(fn, what="测试", attempts=3, delay=0)
        assert len(calls) == 1

    def test_success_on_second_attempt(self):
        calls: list[int] = []

        def fn():
            calls.append(1)
            if len(calls) < 2:
                raise ApiError("暂时失败", retryable=True)
            return "ok"

        assert call_with_retry(fn, what="测试", attempts=3, delay=0) == "ok"
        assert len(calls) == 2


class TestRetryFeedback:
    """Y6：重试必须可见，否则最坏情况静默挂起 3 分钟。"""

    def test_on_retry_is_called(self):
        events: list[tuple[int, int, str]] = []

        def fn():
            raise ApiError("云端超时", retryable=True)

        with pytest.raises(ApiError):
            call_with_retry(
                fn, what="测试", attempts=3, delay=0,
                on_retry=lambda a, b, r: events.append((a, b, r)),
            )
        assert events == [(1, 3, "云端超时"), (2, 3, "云端超时")]

    def test_on_retry_exception_does_not_break_retry(self):
        """订阅方抛异常不该拖垮重试循环。"""
        fn, calls = _counter(lambda: ApiError("失败", retryable=True))

        def boom(a, b, r):
            raise RuntimeError("订阅方自己炸了")

        with pytest.raises(ApiError):
            call_with_retry(fn, what="测试", attempts=3, delay=0, on_retry=boom)
        assert len(calls) == 3


class TestHttpClientTranslation:
    """HttpClient 把 requests 异常翻译成 ApiError 时必须带上 retryable。"""

    def _client(self) -> HttpClient:
        client = HttpClient(Config.load({}))
        client._session = MagicMock()
        return client

    def test_connection_error_is_retryable(self):
        client = self._client()
        client._session.post.side_effect = requests.ConnectionError("DNS 失败")
        with pytest.raises(ApiError) as exc_info:
            client.post_json("https://example.invalid", {})
        assert exc_info.value.retryable is True, "网络异常必须标记为可重试"

    def test_timeout_is_retryable(self):
        client = self._client()
        client._session.post.side_effect = requests.Timeout("超时")
        with pytest.raises(ApiError) as exc_info:
            client.post_json("https://example.invalid", {})
        assert exc_info.value.retryable is True
        assert exc_info.value.status_code == 408

    def test_get_bytes_connection_error_is_retryable(self):
        client = self._client()
        client._session.get.side_effect = requests.ConnectionError("连接被拒")
        with pytest.raises(ApiError) as exc_info:
            client.get_bytes("https://example.invalid/a.wav")
        assert exc_info.value.retryable is True
