"""工具抽象。

## 一个工具要回答三个问题

    name         模型用它来指名道姓地调用
    description  模型**靠它决定要不要调** —— 这是最容易被写坏的一环
    parameters   参数的 JSON Schema，模型靠它决定填什么

description 写得好不好，直接决定工具会不会在该用的时候被用上。
所以它必须描述**使用场景**（「用户问天气、气温、要不要带伞时使用」），
而不只是描述功能（「查询天气」）—— 后者模型不知道该在什么时候调。

## 失败是结果，不是异常

工具的 `run()` 可以抛 `ToolError`（预期内的失败，例如城市名没查到）。
**注册表**会把任何异常转成一条 `{"error": ...}` 的结果回填给模型，
让它自己决定怎么跟用户说。

这条纪律的理由：查天气超时是很平常的事，但如果它让整轮对话失败，
用户听到的就是「出错了」而不是「我查不到天气」。前者像坏了，后者像人话。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.core.errors import AyuanError


class ToolError(AyuanError):
    """工具执行失败。

    抛它的地方应该是**能预料到的**失败：城市查不到、参数不合法、
    上游返回 4xx。预料之外的异常由注册表兜底，一样会变成工具结果，
    但会额外记一条 exception 日志 —— 那是 bug，不是业务。
    """


@dataclass(frozen=True)
class ToolResult:
    """一次工具调用的结果。**永远不抛异常**，失败也在这里表达。"""

    ok: bool
    content: str

    @property
    def is_error(self) -> bool:
        return not self.ok

    def as_message(self) -> str:
        """回填给模型的内容。"""
        return self.content


class Tool(ABC):
    """一个可被模型调用的工具。"""

    #: 模型调用时用的名字。用 snake_case，与常见习惯一致。
    name: str = ""
    #: **模型靠它决定要不要调**。写清使用场景，不要只写功能。
    description: str = ""
    #: 参数的 JSON Schema
    parameters: dict[str, Any] = {}
    #: 单次执行的墙钟上限（秒）。工具的 I/O 自己也该带超时，
    #: 这里是最后一道保险 —— 一个卡死的工具会把整个助手拖住。
    timeout: float = 15.0

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI `tools` 参数里的那一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def run(self, **kwargs: Any) -> str:
        """执行并返回**给模型看的内容**。

        返回字符串而不是结构化对象：内容最终要作为 tool 消息回填给模型，
        而模型读 JSON 和读人话都没问题。工具自己决定哪种更省 token、
        更不容易被误读。

        Raises:
            ToolError: 预期内的失败
        """
        raise NotImplementedError
