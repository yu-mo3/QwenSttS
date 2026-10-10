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
from app.config import Config, mask_secret
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
    ("M2", "L4 能力层：语音识别 → 对话 → 语音合成", "done"),
    ("M3", "L3+L5：离线唤醒 · VAD 断句 · 连续对话 · 打断", "done"),
    ("M4", "L6 表现层：PySide6 桌面界面", "doing"),
    ("M5", "视觉理解 · Agent 工具池 · 打包分发", "todo"),
]

_STATUS_MARK = {"done": "✅", "doing": "🚧", "todo": "⏳"}


def show_status(config: Config) -> None:
    """打印当前配置与能力状态。"""
    print(BANNER)

    print("  ── 配置 ──────────────────────────────────────────")
    print(f"     版本       : {__version__}")
    print(f"     百炼 API   : {'✅ ' + mask_secret(config.secrets.dashscope_api_key) if config.secrets.has_dashscope else '❌ 未配置，云端能力不可用'}")
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


def _device_line(dev, *, is_default: bool) -> str:
    """一行设备描述，带状态标记。"""
    marks = []
    if is_default:
        marks.append("系统默认")
    if dev.is_handsfree:
        marks.append("免提模式·音质差")
    suffix = f"   ← {'，'.join(marks)}" if marks else ""
    return f"        [{dev.index:>2}] {dev.name}{suffix}"


def show_devices(config: Config, *, show_all: bool = False) -> None:
    """打印音频设备与推荐路由。

    路由知识（免提规避策略）住在 app/audio/devices.py —— 那是业务知识，
    不是诊断脚本的私货，编排层将来也要用同一套。
    """
    from app.audio.devices import PREFERRED_HOSTAPI, default_device, list_devices, suggest_route

    print("  ── 音频设备 ──────────────────────────────────────")
    try:
        all_devices = list_devices()
    except Exception as exc:
        print(f"     ❌ 设备枚举失败：{exc}")
        print()
        return

    shown = all_devices if show_all else [d for d in all_devices if d.hostapi == PREFERRED_HOSTAPI]
    hidden = len(all_devices) - len(shown)
    din = default_device(want_input=True)
    dout = default_device(want_input=False)

    print("     🎤 输入：")
    for dev in shown:
        if dev.is_input:
            print(_device_line(dev, is_default=bool(din and dev.index == din.index)))

    print("     🔊 输出：")
    for dev in shown:
        if dev.is_output:
            print(_device_line(dev, is_default=bool(dout and dev.index == dout.index)))

    if hidden > 0 and not show_all:
        print(f"     （另有 {hidden} 个端点属于其它驱动接口，已折叠；加 --all-devices 查看全部）")

    route = suggest_route(all_devices)
    print()
    print("     💡 推荐路由")
    if route.input_device:
        print(f"        麦克风 → {route.input_device.name}（index {route.input_device.index}）")
    if route.output_device:
        print(f"        播放   → {route.output_device.name}（index {route.output_device.index}）")
    print(f"        理由：{route.reason}")
    print()


def cmd_check(config: Config, args: argparse.Namespace) -> int:
    """运行环境自检（转发给 tools/check_env.py）。

    必须把参数**透传**过去 —— 否则无麦克风的机器（CI、服务器、
    VSCode Remote）永远跑不过自检，而 check_env 明明支持 --no-record。
    """
    import runpy

    script = PROJECT_ROOT / "tools" / "check_env.py"
    if not script.exists():
        log.error("找不到自检脚本：%s", script)
        return 1

    forwarded = [str(script)]
    if args.no_record:
        forwarded.append("--no-record")
    if args.all_devices:
        forwarded.append("--all-devices")
    if args.seconds is not None:
        forwarded += ["--seconds", str(args.seconds)]

    previous_argv = sys.argv
    sys.argv = forwarded
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = previous_argv
    return 0


def cmd_run(config: Config) -> int:
    """启动阿源主体。"""
    show_status(config)
    show_devices(config)

    print("  ── 状态 ──────────────────────────────────────────")
    print("     ✅ 地基与能力层已就绪：阿源已经能听、能说、能聊。")
    print()
    print("     现在可以做的：")
    print("       · 语音对话       →  python tools/m2_chat.py")
    print("       · 纯文字对话     →  python tools/m2_chat.py --text")
    print("       · 运行环境自检   →  python -m app.main --check")
    print("       · 查看音频设备   →  python -m app.main --devices")
    print()
    print("       · 免按钮对话     →  python tools/m3_assistant.py")
    print()
    print("     图形界面       →  python -m app.ui   （或 VSCode 按 F5）")
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
    parser.add_argument("--no-record", action="store_true", dest="no_record", help="自检时跳过录音与回放（无麦克风环境）")
    parser.add_argument("--all-devices", action="store_true", dest="all_devices", help="显示全部驱动接口下的设备端点")
    parser.add_argument("--seconds", type=float, default=None, help="自检录音时长（秒）")
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
        return cmd_check(config, args)

    if args.devices:
        show_devices(config, show_all=args.all_devices)
        return 0

    return cmd_run(config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n阿源已退出。")
        sys.exit(130)
