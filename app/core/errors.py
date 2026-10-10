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
    """云端接口调用失败。"""

    def __init__(self, message: str, *, hint: str = "", status_code: int | None = None) -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code
