"""阿源 · 应用主入口。

用法：
    python -m app.main              启动阿源
    python -m app.main --check      先做一次环境自检，再启动
    python -m app.main --version    显示版本

在 VSCode 中直接按 F5 即可启动（调试配置见 .vscode/launch.json）。

分层约定：
    本文件只负责「解析参数 → 组装依赖 → 交给编排层」，不写业务逻辑。
    M2 起，语音对话主循环接入 app/orchestrator/。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许两种启动方式：python -m app.main / python app/main.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import __version__
from app.config import Config
from app.core.logger import ensure_utf8_stdout, get_logger, setup_logging

log = get_logger("ayuan")

BANNER = """
  ┌───────────────────────────────────────────────────┐
  │                                                   │
  │              阿  源   ·   QwenSttS                │
  │                                                   │
  │        一个能听、能说、能看的多模态助手            │
  │                                                   │
  └───────────────────────────────────────────────────┘
"""

# 里程碑状态：(编号, 名称, 状态)  状态取值 done / doing / todo
MILESTONES = [
    ("M1", "工程骨架 · L1 基础层 · L2 设备层", "done"),
    ("M2", "L4 能力层：语音识别 → 对话 → 语音合成", "doing"),
    ("M3", "L3+L5：离线唤醒 · VAD 断句 · 连续对话 · 打断", "todo"),
    ("M4", "L6 表现层：PySide6 桌面界面", "todo"),
    ("M5", "视觉理解 · Agent 工具池 · 打包分发", "todo"),
]

_STATUS_MARK = {"done": "✅", "doing": "🚧", "todo": "⏳"}


def _mask(secret: str) -> str:
    """密钥脱敏显示。"""
    if not secret:
        return "未配置"
    return f"{secret[:8]}…{secret[-4:]}（共 {len(secret)} 字符）"


def show_status(config: Config) -> None:
    """打印当前配置与能力状态。"""
    print(BANNER)

    print("  ── 配置 ──────────────────────────────────────────")
    print(f"     版本       : {__version__}")
    print(f"     百炼 API   : {'✅ ' + _mask(config.secrets.dashscope_api_key) if config.secrets.has_dashscope else '❌ 未配置，云端能力不可用'}")
    print(f"     天气 API   : {'✅ 已配置' if config.secrets.has_weather else '⚠️  未配置（M5 才需要）'}")
    print(f"     对话模型   : {config.model.llm}（思考模式：{'开' if config.model.enable_thinking else '关'}）")
    print(f"     识别 / 合成: {config.model.asr} / {config.model.tts} · 音色 {config.model.tts_voice}")
    print(f"     音频参数   : {config.audio.sample_rate}Hz / {config.audio.channels}声道 / {config.audio.sample_width * 8}bit")
    print()

    print("  ── 里程碑 ────────────────────────────────────────")
    for code, name, status in MILESTONES:
        mark = _STATUS_MARK[status]
        arrow = "  ← 下一个" if status == "doing" else ""
        print(f"     {mark} {code}  {name}{arrow}")
    print()


def show_devices(config: Config) -> None:
    """打印音频设备与推荐路由。"""
    from app.audio.devices import default_device, list_devices

    print("  ── 音频设备 ──────────────────────────────────────")
    try:
        devices = [d for d in list_devices() if d.hostapi == "MME"]
    except Exception as exc:
        print(f"     ❌ 设备枚举失败：{exc}")
        print()
        return

    din = default_device(want_input=True)
    dout = default_device(want_input=False)

    print("     🎤 输入：")
    for dev in devices:
        if dev.is_input:
            mark = "  ← 系统默认" if din and dev.index == din.index else ""
            print(f"        [{dev.index:>2}] {dev.name}{mark}")
    print("     🔊 输出：")
    for dev in devices:
        if dev.is_output:
            mark = "  ← 系统默认" if dout and dev.index == dout.index else ""
            print(f"        [{dev.index:>2}] {dev.name}{mark}")
    print()


def cmd_check(config: Config) -> int:
    """运行环境自检（转发给 tools/check_env.py）。"""
    import runpy

    script = PROJECT_ROOT / "tools" / "check_env.py"
    if not script.exists():
        log.error("找不到自检脚本：%s", script)
        return 1
    sys.argv = [str(script)]
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def cmd_run(config: Config) -> int:
    """启动阿源主体。"""
    show_status(config)
    show_devices(config)

    print("  ── 状态 ──────────────────────────────────────────")
    print("     🚧 当前处于 M1 阶段：地基已就绪，阿源还没有「大脑」。")
    print()
    print("     现在可以做的：")
    print("       · 运行环境自检    →  python -m app.main --check")
    print("       · 查看音频设备    →  python -m app.main --devices")
    print()
    print("     下一步 M2 将接入：语音识别 → 对话 → 语音合成。")
    print("     届时直接运行 python -m app.main 就能和她对话。")
    print()
    print("  ──────────────────────────────────────────────────")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ayuan",
        description="阿源 · 多模态智能语音助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m app.main            启动阿源\n"
            "  python -m app.main --check    环境自检\n"
            "  python -m app.main --devices  只看音频设备\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"阿源 QwenSttS {__version__}")
    parser.add_argument("--check", action="store_true", help="先运行环境自检，再退出")
    parser.add_argument("--devices", action="store_true", help="只打印音频设备与推荐路由")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    # 必须先于 argparse：--version 会在 setup_logging() 之前打印并退出
    ensure_utf8_stdout()

    args = build_parser().parse_args(argv)
    config = Config.load()

    setup_logging("DEBUG" if args.verbose else config.log_level)
    config.ensure_dirs()

    if args.check:
        return cmd_check(config)

    if args.devices:
        show_devices(config)
        return 0

    return cmd_run(config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n阿源已退出。")
        sys.exit(130)
