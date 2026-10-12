"""Agent 工具池的纯函数与隔离测试。

**不联网** —— 天气工具用假的 requests 层驱动。
真实调用由 tools/m5_weather.py 人工跑。
"""

from __future__ import annotations

import json
import time

import pytest

from app.agent import Tool, ToolError, ToolRegistry, WeatherTool, build_registry
from app.agent.registry import MAX_TOOL_CALLS_PER_TURN
from app.config import Config
from app.core.errors import AyuanError


class EchoTool(Tool):
    name = "echo"
    description = "把输入原样返回"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def run(self, text: str = "", **_) -> str:
        return f"echo: {text}"


class BoomTool(Tool):
    name = "boom"
    description = "总是抛未预料的异常"
    parameters = {"type": "object", "properties": {}}

    def run(self, **_) -> str:
        raise RuntimeError("内部炸了")


class SlowTool(Tool):
    name = "slow"
    description = "睡很久"
    parameters = {"type": "object", "properties": {}}
    timeout = 0.15

    def run(self, **_) -> str:
        time.sleep(3.0)
        return "终于好了"


class ExpectedFailTool(Tool):
    name = "expected"
    description = "抛一个预期内的失败"
    parameters = {"type": "object", "properties": {}}

    def run(self, **_) -> str:
        raise ToolError("这个东西查不到", hint="换个说法试试")


# ══════════════════════════════════════════════════════
#  注册表
# ══════════════════════════════════════════════════════

class TestRegistryBasics:
    def test_register_and_lookup(self):
        registry = ToolRegistry([EchoTool()])
        assert registry.names == ("echo",)
        assert isinstance(registry.get("echo"), EchoTool)
        assert registry.get("不存在") is None

    def test_duplicate_name_is_rejected(self):
        """重名会让「调的到底是哪个」变得说不清，宁可启动就炸。"""
        registry = ToolRegistry([EchoTool()])
        with pytest.raises(ValueError):
            registry.register(EchoTool())

    def test_tool_without_name_is_rejected(self):
        class Nameless(Tool):
            def run(self, **_) -> str:
                return ""

        with pytest.raises(ValueError):
            ToolRegistry([Nameless()])

    def test_empty_registry_is_falsy(self):
        assert not ToolRegistry()
        assert ToolRegistry([EchoTool()])

    def test_schemas_are_openai_shaped(self):
        schema = ToolRegistry([EchoTool()]).schemas()[0]
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "echo"
        assert schema["function"]["parameters"]["required"] == ["text"]

    def test_missing_required(self):
        tool = EchoTool()
        assert ToolRegistry.missing_required(tool, {}) == ["text"]
        assert ToolRegistry.missing_required(tool, {"text": ""}) == ["text"]
        assert ToolRegistry.missing_required(tool, {"text": "hi"}) == []


class TestExecutionNeverRaises:
    """整个工具池最重要的一条纪律。

    「查天气超时」时用户想听的是「我查不到」，而不是「出错了」。
    所以 execute() 必须永不抛异常 —— 任何失败都变成一条结果。
    """

    def test_success(self):
        result = ToolRegistry([EchoTool()]).execute("echo", {"text": "你好"})
        assert result.ok and "你好" in result.content

    def test_unknown_tool_lists_the_available_ones(self):
        """模型经常只是拼错了名字 —— 告诉它有哪些可选，它下一次就对了。"""
        result = ToolRegistry([EchoTool()]).execute("echoo", {})
        assert result.is_error
        assert "echo" in result.content

    def test_unknown_tool_when_registry_is_empty(self):
        result = ToolRegistry().execute("anything", {})
        assert result.is_error

    def test_arguments_as_json_string_are_parsed(self):
        """模型偶尔把参数写成 JSON 字符串而不是对象。"""
        result = ToolRegistry([EchoTool()]).execute("echo", '{"text": "hi"}')
        assert result.ok

    def test_malformed_json_arguments(self):
        result = ToolRegistry([EchoTool()]).execute("echo", "{不是 json")
        assert result.is_error and "JSON" in result.content

    @pytest.mark.parametrize("bad", [None, 42, ["a"], "   "])
    def test_non_object_arguments(self, bad):
        result = ToolRegistry([EchoTool()]).execute("echo", bad)
        assert result.is_error

    def test_missing_required_tells_the_model_what_is_missing(self):
        result = ToolRegistry([EchoTool()]).execute("echo", {})
        assert result.is_error
        assert "location" not in result.content
        assert "text" in result.content

    def test_expected_failure_becomes_a_result(self):
        result = ToolRegistry([ExpectedFailTool()]).execute("expected", {})
        assert result.is_error
        payload = json.loads(result.content)
        assert "查不到" in payload["error"]
        assert payload["hint"]

    def test_unexpected_exception_becomes_a_result_not_a_traceback(self):
        result = ToolRegistry([BoomTool()]).execute("boom", {})
        assert result.is_error
        # 不该把 RuntimeError 的原文甩给模型
        assert "内部炸了" not in result.content

    def test_timeout_is_enforced(self):
        """一个卡死的工具会把整个助手拖住 —— 必须有终止条件。"""
        registry = ToolRegistry([SlowTool()])
        started = time.monotonic()
        result = registry.execute("slow", {})
        elapsed = time.monotonic() - started
        assert result.is_error
        assert elapsed < 1.0, f"应当在 {SlowTool.timeout}s 左右就放弃，实际 {elapsed:.2f}s"

    def test_tool_error_is_an_ayuan_error(self):
        """工具的异常必须属于 AyuanError 家族，否则上层容错兜不住。"""
        assert issubclass(ToolError, AyuanError)


# ══════════════════════════════════════════════════════
#  天气工具
# ══════════════════════════════════════════════════════

class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False)

    def json(self):
        return self._body


NOW_BODY = {
    "results": [
        {
            "location": {"id": "WX4FBXXFKE4F", "name": "北京", "timezone": "Asia/Shanghai"},
            "now": {"text": "晴", "code": "0", "temperature": "25"},
            "last_update": "2026-10-12T13:00:00+08:00",
        }
    ]
}

DAILY_BODY = {
    "results": [
        {
            "location": {"name": "北京"},
            "daily": [
                {
                    "date": "2026-10-13",
                    "text_day": "小雨",
                    "text_night": "阴",
                    "high": "20",
                    "low": "12",
                    "precip": "3.20",
                    "wind_direction": "东北",
                    "humidity": "88",
                }
            ],
        }
    ]
}


@pytest.fixture
def weather(monkeypatch):
    """装一个假的 requests.get，按 path 分发响应。"""
    calls: list[dict] = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, "params": params or {}, "timeout": timeout})
        if url.endswith("now.json"):
            return FakeResponse(200, NOW_BODY)
        if url.endswith("daily.json"):
            return FakeResponse(200, DAILY_BODY)
        return FakeResponse(404, {"status_code": "AP010005"})

    monkeypatch.setattr("app.agent.weather.requests.get", fake_get)
    tool = WeatherTool(Config.load({"WEATHER_API_KEY": "fake-key"}))
    return tool, calls


class TestWeatherTool:
    def test_now_only_by_default(self, weather):
        tool, calls = weather
        payload = json.loads(tool.run(location="北京"))
        assert payload["now"]["weather"] == "晴"
        assert payload["now"]["temperature_c"] == "25"
        assert "forecast" not in payload
        assert len(calls) == 1, "只要实况时不该多打一次预报接口"

    def test_forecast_when_asked(self, weather):
        tool, calls = weather
        payload = json.loads(tool.run(location="北京", days=1))
        assert payload["forecast"][0]["day_weather"] == "小雨"
        # 降水量是「要不要带伞」唯一的依据，不能丢
        assert payload["forecast"][0]["precip_mm"] == "3.20"
        assert len(calls) == 2

    def test_days_is_clamped(self, weather):
        tool, calls = weather
        tool.run(location="北京", days=99)
        assert calls[1]["params"]["days"] == 3

    def test_negative_days_means_now_only(self, weather):
        tool, calls = weather
        tool.run(location="北京", days=-5)
        assert len(calls) == 1

    def test_garbage_days_falls_back_to_zero(self, weather):
        tool, calls = weather
        tool.run(location="北京", days="明天")
        assert len(calls) == 1

    def test_irrelevant_fields_are_stripped(self, weather):
        """城市 id、时区这些回给模型只会白占 token，
        还可能诱导它把 WX4FBXXFKE4F 念出来。"""
        tool, _ = weather
        payload = json.loads(tool.run(location="北京"))
        assert "WX4FBXXFKE4F" not in json.dumps(payload)
        assert "timezone" not in json.dumps(payload)

    def test_key_is_sent_but_not_echoed_back(self, weather):
        """密钥要发给服务端，但绝不能出现在回报给模型的内容里 ——
        那段内容会被写进对话记忆、进日志、最终可能被截图出去。"""
        tool, calls = weather
        payload = tool.run(location="北京")
        assert calls[0]["params"]["key"] == "fake-key"
        assert "fake-key" not in payload

    def test_timeout_is_passed_to_requests(self, weather):
        """工具的墙钟上限是最后一道保险；真正先触发的是 HTTP 自己的超时。"""
        tool, calls = weather
        tool.run(location="北京")
        assert calls[0]["timeout"] == 10

    def test_missing_key_raises_tool_error(self, monkeypatch):
        tool = WeatherTool(Config.load({}))
        with pytest.raises(ToolError):
            tool.run(location="北京")

    def test_empty_location_raises_tool_error(self, weather):
        tool, _ = weather
        with pytest.raises(ToolError):
            tool.run(location="   ")

    def test_city_not_found_is_explained(self, monkeypatch):
        monkeypatch.setattr(
            "app.agent.weather.requests.get",
            lambda *a, **k: FakeResponse(404, {"status_code": "AP010010"}),
        )
        tool = WeatherTool(Config.load({"WEATHER_API_KEY": "k"}))
        with pytest.raises(ToolError) as info:
            tool.run(location="不存在的城市")
        assert "城市" in info.value.hint

    def test_bad_key_is_explained(self, monkeypatch):
        monkeypatch.setattr(
            "app.agent.weather.requests.get",
            lambda *a, **k: FakeResponse(403, {"status_code": "AP010003"}),
        )
        tool = WeatherTool(Config.load({"WEATHER_API_KEY": "k"}))
        with pytest.raises(ToolError) as info:
            tool.run(location="北京")
        assert "WEATHER_API_KEY" in info.value.hint

    def test_network_error_becomes_tool_error(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.ConnectionError("网线掉了")

        monkeypatch.setattr("app.agent.weather.requests.get", boom)
        tool = WeatherTool(Config.load({"WEATHER_API_KEY": "k"}))
        with pytest.raises(ToolError):
            tool.run(location="北京")

    def test_malformed_body_becomes_tool_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.agent.weather.requests.get",
            lambda *a, **k: FakeResponse(200, {"results": []}),
        )
        tool = WeatherTool(Config.load({"WEATHER_API_KEY": "k"}))
        with pytest.raises(ToolError):
            tool.run(location="北京")

    def test_end_to_end_through_the_registry(self, weather):
        """走注册表时，失败必须变成结果而不是异常。"""
        tool, _ = weather
        registry = ToolRegistry([tool])
        assert registry.execute("get_weather", {"location": "北京"}).ok
        assert registry.execute("get_weather", {}).is_error


# ══════════════════════════════════════════════════════
#  装配
# ══════════════════════════════════════════════════════

class TestBuildRegistry:
    def test_default_pool_has_the_weather_tool(self):
        registry = build_registry(Config.load({}))
        assert "get_weather" in registry.names

    def test_tool_call_cap_is_defined(self):
        assert MAX_TOOL_CALLS_PER_TURN >= 1
