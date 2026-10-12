"""L4 能力层 · Agent 工具池。

工具（Tool）与 asr/llm/tts 那种「一个端点一个类」不是一回事：
它还包含**注册表、参数 schema 生成、调用分发、超时与错误隔离**。
所以单独成包，而不是塞进 app/capabilities/。

## 一条贯穿始终的纪律

**失败是结果，不是异常。** 工具执行失败会被注册表转成一条
error 结果回填给模型，让它自己决定怎么跟用户说 ——
因为「查天气超时」时用户想听的是「我查不到」，而不是「出错了」。
"""

from __future__ import annotations

from app.agent.base import Tool, ToolError, ToolResult
from app.agent.registry import MAX_TOOL_CALLS_PER_TURN, ToolRegistry
from app.agent.weather import WeatherTool
from app.config import Config

__all__ = [
    "Tool",
    "ToolError",
    "ToolResult",
    "ToolRegistry",
    "WeatherTool",
    "MAX_TOOL_CALLS_PER_TURN",
    "build_registry",
]


def build_registry(config: Config) -> ToolRegistry:
    """装配默认工具池。

    写成工厂函数而不是在 import 期建：注册表要拿 Config
    （里面有天气密钥），而 import 期不该依赖配置已经加载好。
    """
    return ToolRegistry([WeatherTool(config)])
