"""L1 基础层：配置、日志、异常。

事件总线属于编排层关注点，将在 M3 随状态机一起引入。
"""

from app.core.errors import (
    ApiError,
    AudioDeviceError,
    AyuanError,
    ConfigError,
)

__all__ = ["AyuanError", "ConfigError", "AudioDeviceError", "ApiError"]
