"""项目统一异常类型。

设计意图：让上层能用「一类异常」兜住「一层的问题」，而不是散落的
except Exception。每条异常都自带人话解释，便于直接展示给用户。
"""

from __future__ import annotations


class AyuanError(Exception):
    """所有阿源异常的基类。"""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message}（{self.hint}）" if self.hint else self.message


class ConfigError(AyuanError):
    """配置缺失或非法 —— 通常是 .env 没填。"""


class AudioDeviceError(AyuanError):
    """音频设备打开失败、被占用或不存在。"""


class ApiError(AyuanError):
    """云端接口调用失败。

    retryable 是**显式的语义位**，而不是让调用方从 status_code 去猜。

    原因（真实踩过的坑）：网络层异常（连接被拒、DNS 失败、连接重置）
    根本没有 HTTP 状态码。只靠 status_code 判断「该不该重试」，这类错误
    会被一律当成不可重试 —— call_with_retry 的 docstring 承诺重试网络抖动，
    实际上**一次都没重试过**。
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str = "",
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code
        self.retryable = retryable
