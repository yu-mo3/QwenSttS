r"""M5 工具池验收 · 问她天气，看她会不会真的去查。

    .venv\Scripts\python.exe tools\m5_weather.py
    .venv\Scripts\python.exe tools\m5_weather.py --ask "明天要带伞吗？我在杭州"
    .venv\Scripts\python.exe tools\m5_weather.py --tool-only   # 只测工具，不走模型

--tool-only 存在的意义：**模型与工具是两段**。任何一段坏了都要能单独定位 ——
如果 --tool-only 正常而整轮不正常，问题在模型那一侧。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent import build_registry  # noqa: E402
from app.capabilities.llm import ChatProvider, ConversationMemory  # noqa: E402
from app.config import Config  # noqa: E402
from app.core.errors import AyuanError  # noqa: E402
from app.core.logger import ensure_utf8_stdout, setup_logging  # noqa: E402

WIDTH = 64

#: 默认连着问几句，覆盖「该调」「不该调」「要预报」三种情形。
DEFAULT_QUESTIONS = [
    "北京今天天气怎么样？",
    "明天出门要带伞吗？我在杭州",
    "你好呀",
]


def head(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def main() -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(prog="m5_weather", description="阿源 · 工具池验收")
    parser.add_argument("--ask", help="只问这一句")
    parser.add_argument("--tool-only", action="store_true", help="只测工具，不调用对话模型")
    args = parser.parse_args()

    config = Config.load()
    setup_logging("INFO")
    registry = build_registry(config)

    head("工具池")
    print(f"  已注册：{'、'.join(registry.names) or '（空）'}")
    print(f"  天气密钥：{'已配置' if config.secrets.weather_api_key else '❌ 未配置'}")
    print(f"  对话模型：{config.model.llm}")

    if args.tool_only:
        head("直接调工具")
        for name, arguments in [
            ("get_weather", {"location": "北京"}),
            ("get_weather", {"location": "杭州", "days": 3}),
            ("get_weather", {"location": "不存在的城市xyz"}),
            ("get_weather", {}),
        ]:
            started = time.monotonic()
            result = registry.execute(name, arguments)
            elapsed = time.monotonic() - started
            mark = "成功" if result.ok else "失败"
            print(f"  [{mark}] {name}({arguments})  {elapsed:.2f}s")
            print(f"         {result.content[:220]}")
        print()
        print("  工具这一侧只要上面有「成功」，就说明取数与错误处理都正常。")
        return 0

    questions = [args.ask] if args.ask else DEFAULT_QUESTIONS
    llm = ChatProvider(config)

    # 数一下每一轮发了几次请求 —— 「不调工具时不该多一次往返」是硬指标
    original = llm._stream_turn
    counter = {"n": 0}

    def counting(messages, *, tools=None, sink=None):
        counter["n"] += 1
        yield from original(messages, tools=tools, sink=sink)

    llm._stream_turn = counting

    memory = ConversationMemory()
    for question in questions:
        counter["n"] = 0
        head(f"问：{question}")
        started = time.monotonic()
        try:
            reply = llm.reply(
                question,
                memory,
                tools=registry,
                on_tool=lambda record: print(f"  [工具] {record.summary()}"),
            )
        except AyuanError as exc:
            print(f"  [失败] {exc.message}")
            if exc.hint:
                print(f"         {exc.hint}")
            return 1

        first = reply.stats.first_byte if reply.stats else None
        print(f"  请求轮数：{counter['n']}")
        print(f"  首字：{first:.2f}s" if first else "  首字：?")
        print(f"  总耗时：{time.monotonic() - started:.2f}s")
        print()
        print(f"  阿源：{reply.text}")

    print()
    print("=" * WIDTH)
    print("  判据：")
    print("    · 问天气的那几轮应当是 2 轮请求，并且有 [工具] 那行")
    print("    · 「你好呀」应当恰好 1 轮请求、没有工具调用")
    print("    · 回复是人话：无 Markdown、无列表符号")
    print("=" * WIDTH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
