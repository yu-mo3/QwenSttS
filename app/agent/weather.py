"""天气工具（心知天气）。

## description 为什么写成那样

模型的工具选择**完全**依赖 description。只写「查询天气」的话，
它不知道「明天要带伞吗」「外面冷不冷」该不该调 —— 而那些恰恰是
语音助手上最常出现的问法。所以 description 里必须列出**使用场景**。

## 返回什么由「用户会怎么问」决定

- 实况带 temperature -> 「现在多少度」
- 预报带 high/low    -> 「要穿外套吗」
- 预报带 precip      -> 「要带伞吗」

少一个字段，就有一类问法答不上来。心知的生活指数 API 在当前套餐下
返回 404，所以不依赖它 —— 需要的信息从 daily 里取。
"""

from __future__ import annotations

import json
from typing import Any

import requests

from app.agent.base import Tool, ToolError
from app.config import Config
from app.core.logger import get_logger

log = get_logger(__name__)

BASE_URL = "https://api.seniverse.com/v3/weather"

#: 单次 HTTP 请求的上限。工具的 timeout 是最后一道保险，
#: 这里才是真正会先触发的那个 —— 10 秒足够，再长用户已经在等了。
REQUEST_TIMEOUT = 10

#: 最多要几天预报。心知免费套餐支持 3 天。
MAX_DAYS = 3

#: 心知的错误码 -> 给模型/用户看的人话
_ERROR_HINTS = {
    "AP010010": "没找到这个城市，换个说法试试（用「北京」「杭州」这样的城市名）",
    "AP010003": "天气服务的密钥无效，请检查 .env 里的 WEATHER_API_KEY",
    "AP010001": "请求参数有问题",
    "AP010005": "当前套餐不支持这个接口",
}


class WeatherTool(Tool):
    """查询中国城市的实时天气与未来几天预报。"""

    name = "get_weather"
    description = (
        "查询某个中国城市的实时天气和未来几天的预报。"
        "当用户问到天气、气温、下雨、要不要带伞、穿什么衣服、"
        "适不适合出门这类问题时使用。"
        "不要在用户只是闲聊或问其它事情时调用。"
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "城市名，用中文，例如「北京」「杭州」。",
            },
            "days": {
                "type": "integer",
                "description": (
                    "要几天的预报。只要当前天气填 0（默认）；"
                    "用户问明天或未来几天时填 1 到 3。"
                ),
                "minimum": 0,
                "maximum": MAX_DAYS,
            },
        },
        "required": ["location"],
    }

    def __init__(self, config: Config) -> None:
        self.config = config

    @property
    def api_key(self) -> str:
        return self.config.secrets.weather_api_key

    # ---------- 执行 ----------

    def run(self, location: str, days: int = 0, **_: Any) -> str:
        if not self.api_key:
            raise ToolError(
                "没有配置天气服务的密钥",
                hint="请在 .env 里设置 WEATHER_API_KEY",
            )

        location = str(location or "").strip()
        if not location:
            raise ToolError("没有指定城市", hint="请告诉我要查哪个城市的天气")

        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 0
        days = max(0, min(days, MAX_DAYS))

        report: dict[str, Any] = {}
        report.update(self._shrink_now(self._fetch("now.json", location=location)))
        if days > 0:
            daily = self._fetch("daily.json", location=location, days=days)
            report["forecast"] = self._shrink_daily(daily)

        return json.dumps(report, ensure_ascii=False)

    # ---------- HTTP ----------

    def _fetch(self, path: str, **params: Any) -> dict[str, Any]:
        """调一次心知接口，返回 results[0]。失败一律抛 ToolError。"""
        query = {"key": self.api_key, "language": "zh-Hans", "unit": "c", **params}
        try:
            response = requests.get(
                f"{BASE_URL}/{path}", params=query, timeout=REQUEST_TIMEOUT
            )
        except requests.Timeout as exc:
            raise ToolError("查天气超时了", hint="网络较慢，稍后再试") from exc
        except requests.RequestException as exc:
            raise ToolError("连不上天气服务", hint=str(exc)) from exc

        if response.status_code != 200:
            message, hint = self._explain(response)
            raise ToolError(message, hint=hint)

        try:
            body = response.json()
        except ValueError as exc:
            raise ToolError(
                "天气服务返回了无法解析的内容", hint=response.text[:200]
            ) from exc

        results = (body or {}).get("results")
        if not isinstance(results, list) or not results:
            raise ToolError(
                "天气服务没有返回结果",
                hint=f"返回结构不符合预期：{str(body)[:200]}",
            )
        return results[0]

    @staticmethod
    def _explain(response: requests.Response) -> tuple[str, str]:
        """把 HTTP 错误翻译成「人能看懂、且知道该怎么办」。"""
        code = ""
        try:
            body = response.json()
            code = str((body or {}).get("status_code") or "")
        except ValueError:
            pass

        hint = _ERROR_HINTS.get(code) or response.text[:200]
        return f"天气服务返回错误（HTTP {response.status_code}）", hint

    # ---------- 瘦身 ----------

    @staticmethod
    def _shrink_now(result: dict[str, Any]) -> dict[str, Any]:
        """只留用得上的字段。

        原始响应里有经纬度、时区偏移、城市 id 这类东西 —— 回给模型
        只会白占 token，还可能诱导它把 WX4FBXXFKE4F 这种 id 念出来。
        """
        location = result.get("location") or {}
        now = result.get("now") or {}
        return {
            "city": location.get("name") or "",
            "updated": result.get("last_update") or "",
            "now": {
                "weather": now.get("text") or "",
                "temperature_c": now.get("temperature") or "",
            },
        }

    @staticmethod
    def _shrink_daily(result: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for day in result.get("daily") or []:
            if not isinstance(day, dict):
                continue
            out.append(
                {
                    "date": day.get("date") or "",
                    "day_weather": day.get("text_day") or "",
                    "night_weather": day.get("text_night") or "",
                    "high_c": day.get("high") or "",
                    "low_c": day.get("low") or "",
                    # 降水量是「要不要带伞」唯一的依据
                    "precip_mm": day.get("precip") or "",
                    "wind": day.get("wind_direction") or "",
                    "humidity": day.get("humidity") or "",
                }
            )
        return out
