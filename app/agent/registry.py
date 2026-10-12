"""工具注册表：注册、查找、导出 schema、执行。

## 这里是「失败不能抛穿」这条纪律的执行者

`execute()` **永不抛异常**。任何失败都变成 `ToolResult(ok=False, content=...)`，
由调用方回填给模型。分三种情况：

    工具名不认识        -> 告诉模型有哪些工具可用（它经常只是拼错了）
    参数不合格          -> 告诉模型缺了哪些必填项（它能自己改对）
    执行抛异常          -> 预料内的（ToolError）说人话；
                          预料外的记 exception 日志，回给模型一句通用说明

## 为什么参数校验要自己做

模型的参数是它自己生成的 JSON，缺必填项、类型写错都很常见。
把「缺 location」这件事明确回给它，下一次调用往往就对了；
不校验的话，工具内部会以一个莫名其妙的 TypeError 收场。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from typing import Any

from app.agent.base import Tool, ToolError, ToolResult
from app.core.logger import get_logger

log = get_logger(__name__)


class ToolRegistry:
    """持有若干个工具。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    # ---------- 注册与查找 ----------

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("工具必须有 name")
        if tool.name in self._tools:
            # 重名会让「调的是哪个」变得说不清，宁可启动就炸
            raise ValueError(f"工具名重复：{tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    @property
    def tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __bool__(self) -> bool:
        return bool(self._tools)

    # ---------- 导出给模型 ----------

    def schemas(self) -> list[dict[str, Any]]:
        """OpenAI `tools` 参数。没有工具时返回空列表。"""
        return [tool.schema() for tool in self._tools.values()]

    # ---------- 参数校验 ----------

    @staticmethod
    def missing_required(tool: Tool, arguments: dict[str, Any]) -> list[str]:
        """列出缺失的必填参数。"""
        required = tool.parameters.get("required") or []
        return [key for key in required if key not in arguments or arguments[key] in (None, "")]

    # ---------- 执行 ----------

    def execute(self, name: str, arguments: Any) -> ToolResult:
        """执行一个工具。**永远不抛异常。**"""
        tool = self._tools.get(name)
        if tool is None:
            known = "、".join(self._tools) or "（当前没有任何工具）"
            log.warning("模型要求调用不存在的工具：%r", name)
            return ToolResult(
                ok=False,
                content=json.dumps(
                    {"error": f"没有名为 {name} 的工具", "available": known},
                    ensure_ascii=False,
                ),
            )

        if isinstance(arguments, str):
            # 模型偶尔会把参数写成 JSON 字符串而不是对象
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return ToolResult(
                    ok=False,
                    content=json.dumps(
                        {"error": "参数不是合法的 JSON", "received": arguments[:200]},
                        ensure_ascii=False,
                    ),
                )
        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                content=json.dumps(
                    {"error": "参数必须是一个对象", "received": str(arguments)[:200]},
                    ensure_ascii=False,
                ),
            )

        missing = self.missing_required(tool, arguments)
        if missing:
            return ToolResult(
                ok=False,
                content=json.dumps(
                    {"error": f"缺少必填参数：{'、'.join(missing)}", "got": list(arguments)},
                    ensure_ascii=False,
                ),
            )

        log.info("调用工具 %s(%s)", name, ", ".join(f"{k}={v!r}" for k, v in arguments.items()))
        return self._run_with_timeout(tool, arguments)

    def _run_with_timeout(self, tool: Tool, arguments: dict[str, Any]) -> ToolResult:
        """带墙钟上限地执行，并把任何异常转成工具结果。"""
        box: dict[str, Any] = {}

        def worker() -> None:
            try:
                box["content"] = tool.run(**arguments)
            except BaseException as exc:  # noqa: BLE001 — 全都要转成结果
                box["error"] = exc

        thread = threading.Thread(
            target=worker, daemon=True, name=f"tool-{tool.name}"
        )
        thread.start()
        # 超时不能阻塞调用方 —— 而工具线程可能还在跑。用 daemon 线程，
        # 超时后它就随进程一起走；这里明确把这个取舍写下来：
        # 「超时了但线程还活着」比「助手被一个卡死的工具冻住」好得多。
        thread.join(tool.timeout)

        if thread.is_alive():
            log.warning("工具 %s 超过 %.1fs 仍未返回，放弃等待", tool.name, tool.timeout)
            return ToolResult(
                ok=False,
                content=json.dumps(
                    {"error": f"{tool.description or tool.name} 查询超时，请稍后再试"},
                    ensure_ascii=False,
                ),
            )

        error = box.get("error")
        if isinstance(error, ToolError):
            log.info("工具 %s 失败（预期内）：%s", tool.name, error.message)
            return ToolResult(
                ok=False,
                content=json.dumps(
                    {"error": error.message, "hint": error.hint},
                    ensure_ascii=False,
                ),
            )
        if error is not None:
            log.exception("工具 %s 抛出未预料的异常", tool.name, exc_info=error)
            return ToolResult(
                ok=False,
                content=json.dumps(
                    {"error": f"工具 {tool.name} 执行失败"},
                    ensure_ascii=False,
                ),
            )

        return ToolResult(ok=True, content=str(box.get("content", "")))


#: 一次对话里最多执行几个工具。模型理论上可以要求调 10 个，
#: 那会让一轮回复等上好几分钟。上限放在编排层（L5）执行，
#: 这个常量只是给出「多少算多」的唯一定义。
MAX_TOOL_CALLS_PER_TURN = 3
