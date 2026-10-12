"""工具调用循环的测试。

**不联网** —— 用假的流式响应驱动。真实模型调用由
tools/m5_weather.py 与人工验收覆盖。

这一层最要紧的断言只有一条：**不调工具的普通聊天，请求轮数必须恰好是 1**。
多一次往返，M2 辛苦压到 0.46s 的首字就白费了。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent import Tool, ToolError, ToolRegistry
from app.agent.registry import MAX_TOOL_CALLS_PER_TURN
from app.capabilities.llm import (
    MAX_TOOL_ROUNDS,
    ChatProvider,
    ConversationMemory,
    _assistant_tool_message,
    _safe_arguments,
)
from app.config import Config
from app.core.errors import ApiError


# ══════════════════════════════════════════════════════
#  假的流式响应
# ══════════════════════════════════════════════════════

def text_chunk(piece: str):
    delta = SimpleNamespace(content=piece, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def call_chunk(index=0, *, id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    call = SimpleNamespace(index=index, id=id, function=function)
    delta = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class FakeCompletions:
    """按脚本依次返回若干「一轮的响应」。记录每次请求的参数。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.rounds:
            return iter([text_chunk("（脚本用完了）")])
        return iter(self.rounds.pop(0))


def make_provider(rounds):
    provider = ChatProvider(Config.load({}))
    completions = FakeCompletions(rounds)
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    return provider, completions


class WeatherishTool(Tool):
    name = "get_weather"
    description = "查天气"
    parameters = {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    }

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

    def run(self, location="", **kwargs):
        self.calls.append({"location": location, **kwargs})
        if self.fail:
            raise ToolError("查不到这个城市", hint="换个说法")
        return json.dumps({"city": location, "now": {"weather": "晴"}}, ensure_ascii=False)


def one_round_text(text: str):
    """把一段话切成若干 chunk，模拟真实的流式返回。"""
    return [text_chunk(text[i : i + 3]) for i in range(0, len(text), 3)]


# ══════════════════════════════════════════════════════
#  核心断言：不调工具时不该多一次往返
# ══════════════════════════════════════════════════════

class TestNoToolMeansNoExtraRoundTrip:
    def test_plain_chat_makes_exactly_one_request(self):
        """M2 辛苦压到 0.46s 的首字，不能因为加了工具就白费。"""
        registry = ToolRegistry([WeatherishTool()])
        provider, completions = make_provider([one_round_text("你好呀，今天怎么样？")])

        reply = provider.reply("你好", ConversationMemory(), tools=registry)

        assert len(completions.requests) == 1
        assert reply.tool_calls == []
        assert "你好呀" in reply.text

    def test_tools_are_offered_on_the_first_request(self):
        registry = ToolRegistry([WeatherishTool()])
        provider, completions = make_provider([one_round_text("嗯")])
        provider.reply("你好", ConversationMemory(), tools=registry)
        assert "tools" in completions.requests[0]
        assert completions.requests[0]["tool_choice"] == "auto"

    def test_no_registry_means_no_tools_parameter(self):
        """不传工具池时，请求里一个 tools 字段都不该有。"""
        provider, completions = make_provider([one_round_text("嗯")])
        provider.reply("你好", ConversationMemory())
        assert "tools" not in completions.requests[0]

    def test_still_streams_and_splits_sentences(self):
        provider, _ = make_provider([one_round_text("第一句。第二句。")])
        reply = provider.reply("你好", ConversationMemory())
        assert reply.sentences == ["第一句。", "第二句。"]


# ══════════════════════════════════════════════════════
#  调工具
# ══════════════════════════════════════════════════════

class TestToolCalling:
    def _two_rounds(self, tool, final="北京今天晴，二十度。"):
        first = [
            call_chunk(0, id="c1", name="get_weather", arguments='{"location":'),
            call_chunk(0, arguments=' "北京"}'),
        ]
        return [first, one_round_text(final)], tool

    def test_calls_the_tool_and_answers(self):
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        rounds, _ = self._two_rounds(tool)
        provider, completions = make_provider(rounds)

        reply = provider.reply("北京天气", ConversationMemory(), tools=registry)

        assert len(completions.requests) == 2
        assert tool.calls == [{"location": "北京"}]
        assert len(reply.tool_calls) == 1
        assert reply.tool_calls[0].ok
        assert "晴" in reply.text

    def test_partial_argument_fragments_are_joined(self):
        """流式协议里 arguments 是一小段一小段来的，必须按 index 拼起来。"""
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        rounds, _ = self._two_rounds(tool)
        provider, _ = make_provider(rounds)
        provider.reply("北京天气", ConversationMemory(), tools=registry)
        assert tool.calls == [{"location": "北京"}], "参数碎片没有拼对"

    def test_tool_result_is_fed_back_to_the_model(self):
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        rounds, _ = self._two_rounds(tool)
        provider, completions = make_provider(rounds)
        provider.reply("北京天气", ConversationMemory(), tools=registry)

        second = completions.requests[1]["messages"]
        assert second[-1]["role"] == "tool"
        assert second[-2]["role"] == "assistant"
        assert second[-2]["tool_calls"][0]["id"] == "c1"
        assert "晴" in second[-1]["content"]

    def test_tool_failure_is_fed_back_not_raised(self):
        """查天气失败时用户想听「我查不到」，而不是「出错了」。"""
        tool = WeatherishTool(fail=True)
        registry = ToolRegistry([tool])
        rounds, _ = self._two_rounds(tool, final="我查不到这个地方的天气。")
        provider, completions = make_provider(rounds)

        reply = provider.reply("火星天气", ConversationMemory(), tools=registry)

        assert len(completions.requests) == 2
        assert not reply.tool_calls[0].ok
        assert "查不到" in completions.requests[1]["messages"][-1]["content"]
        assert "查不到" in reply.text

    def test_on_tool_callback_fires(self):
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        rounds, _ = self._two_rounds(tool)
        provider, _ = make_provider(rounds)

        seen = []
        provider.reply("北京天气", ConversationMemory(), tools=registry, on_tool=seen.append)
        assert len(seen) == 1 and seen[0].name == "get_weather"

    def test_broken_callback_does_not_break_the_reply(self):
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        rounds, _ = self._two_rounds(tool)
        provider, _ = make_provider(rounds)

        def boom(_record):
            raise RuntimeError("界面炸了")

        reply = provider.reply("北京天气", ConversationMemory(), tools=registry, on_tool=boom)
        assert reply.text

    def test_two_different_tools_in_one_round(self):
        class ClockTool(Tool):
            name = "get_time"
            description = "查时间"
            parameters = {"type": "object", "properties": {}}

            def run(self, **_):
                return "12:00"

        weather = WeatherishTool()
        registry = ToolRegistry([weather, ClockTool()])
        first = [
            call_chunk(0, id="a", name="get_weather", arguments='{"location":"北京"}'),
            call_chunk(1, id="b", name="get_time", arguments="{}"),
        ]
        provider, completions = make_provider([first, one_round_text("都查好了。")])

        reply = provider.reply("北京几点，天气如何", ConversationMemory(), tools=registry)
        assert len(reply.tool_calls) == 2

        # 一轮里调两个工具会追加 1 条 assistant + 2 条 tool，
        # 所以按角色找而不是按位置猜
        sent = completions.requests[1]["messages"]
        assistant = [m for m in sent if m["role"] == "assistant"][-1]
        tool_messages = [m for m in sent if m["role"] == "tool"]
        assert len(assistant["tool_calls"]) == 2
        assert [m["tool_call_id"] for m in tool_messages] == ["a", "b"]
        assert len(tool_messages) == 2


# ══════════════════════════════════════════════════════
#  上限：宁可答案不完整，也不能转不出来
# ══════════════════════════════════════════════════════

class TestCaps:
    def test_tool_rounds_are_capped(self):
        """模型一直要工具也不能无限循环下去。"""
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        loop = [call_chunk(0, id="x", name="get_weather", arguments='{"location":"北京"}')]
        provider, completions = make_provider([loop, loop, loop, loop])

        provider.reply("天气", ConversationMemory(), tools=registry)

        assert len(completions.requests) <= MAX_TOOL_ROUNDS + 1

    def test_tool_calls_per_turn_are_capped(self):
        tool = WeatherishTool()
        registry = ToolRegistry([tool])
        many = [
            call_chunk(i, id=f"c{i}", name="get_weather", arguments='{"location":"北京"}')
            for i in range(MAX_TOOL_CALLS_PER_TURN + 3)
        ]
        provider, _ = make_provider([many, one_round_text("好了。")])

        reply = provider.reply("天气", ConversationMemory(), tools=registry)
        assert len(reply.tool_calls) <= MAX_TOOL_CALLS_PER_TURN

    def test_tool_calls_without_a_registry_are_ignored(self):
        """协议上不该发生，但静默丢掉比报错更糟 —— 至少留条日志。"""
        provider, completions = make_provider(
            [[call_chunk(0, id="x", name="get_weather", arguments="{}")]]
        )
        reply = provider.reply("天气", ConversationMemory())   # 没给 tools
        assert len(completions.requests) == 1
        assert reply.tool_calls == []


# ══════════════════════════════════════════════════════
#  消息构造与参数解析（纯函数）
# ══════════════════════════════════════════════════════

class TestMessageShapes:
    def test_assistant_message_shape(self):
        message = _assistant_tool_message(
            "", [{"id": "c1", "name": "get_weather", "arguments": '{"location":"北京"}'}]
        )
        assert message["role"] == "assistant"
        assert message["content"] is None, "只有工具调用时 content 该是 None"
        assert message["tool_calls"][0]["type"] == "function"
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"

    def test_assistant_message_keeps_preamble_text(self):
        """模型有时先说一句「让我查一下」再调工具 —— 那句话要留着。"""
        message = _assistant_tool_message("让我查一下。", [{"id": "c1", "name": "t", "arguments": "{}"}])
        assert message["content"] == "让我查一下。"

    def test_missing_id_gets_a_fallback(self):
        message = _assistant_tool_message("", [{"name": "t", "arguments": "{}"}])
        assert message["tool_calls"][0]["id"]

    def test_safe_arguments(self):
        assert _safe_arguments('{"a": 1}') == {"a": 1}
        assert _safe_arguments("") == {}
        assert "_原始" in _safe_arguments("{不是 json")
        assert "_原始" in _safe_arguments('["数组"]')


class TestMemoryIntegration:
    def test_reply_is_recorded_in_memory(self):
        provider, _ = make_provider([one_round_text("你好呀。")])
        memory = ConversationMemory()
        provider.reply("你好", memory)
        assert memory.turn_count == 1

    def test_empty_input_is_rejected(self):
        provider, _ = make_provider([])
        with pytest.raises(ApiError):
            provider.reply("   ", ConversationMemory())
