"""M1 验收脚本 · 阿源环境自检。

双击 run.bat 即可运行，它会依次检查：
  1. 运行环境（Python 版本、依赖是否齐全）
  2. 配置与密钥（.env 是否就位）
  3. 音频设备（列出端点并给出推荐路由）
  4. 录音（实时显示音量，确认麦克风真的在工作）
  5. 回放（把刚录的放出来，确认扬声器真的在工作）

通过标准：第 5 步能听到自己刚才说的话。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接以脚本方式运行（python tools/check_env.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BANNER_WIDTH = 62

# 项目内部依赖必须放在 sys.path 调整之后
from app.audio.devices import (  # noqa: E402
    PREFERRED_HOSTAPI,
    is_handsfree,
    list_devices,
    suggest_route,
)
from app.config import ENV_FILE, Config, mask_secret  # noqa: E402
from app.core.logger import ensure_utf8_stdout  # noqa: E402

# 依赖清单：(导入名, pip 包名, 用途, 是否 M1 必需)
DEPENDENCIES = [
    ("sounddevice", "sounddevice", "音频采集与播放", True),
    ("numpy", "numpy", "PCM 帧运算", True),
    ("dotenv", "python-dotenv", "配置加载", True),
    ("pydub", "pydub", "音频格式转换", False),
    ("requests", "requests", "云端接口调用", False),
    ("openai", "openai", "LLM / VLM 调用", False),
]

# 检查项三态：True 通过 / False 失败 / None 跳过
CheckResult = bool | None


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文与制表符会乱码。

    复用 app.core.logger 的实现 —— 同一段逻辑不该写两遍。
    """
    ensure_utf8_stdout()


def _supports_color() -> bool:
    """是否值得输出 ANSI 颜色。

    重定向到文件时 isatty() 为 False，此时写转义序列只会污染日志。
    """
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def banner(title: str) -> None:
    print()
    print("═" * BANNER_WIDTH)
    print(f"  {title}")
    print("═" * BANNER_WIDTH)


def step(index: int, total: int, title: str) -> None:
    print()
    print(f"【{index}/{total}】{title}")
    print("─" * BANNER_WIDTH)


def ok(message: str) -> None:
    print(f"  ✅ {message}")


def warn(message: str) -> None:
    print(f"  ⚠️  {message}")


def fail(message: str) -> None:
    print(f"  ❌ {message}")


def info(message: str = "") -> None:
    print(f"     {message}")


# ══════════════════════════════════════════════════════════
#  第 1 步：运行环境
# ══════════════════════════════════════════════════════════

def check_runtime() -> bool:
    step(1, 5, "运行环境")

    version = sys.version_info
    version_text = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) in {(3, 11), (3, 12)}:
        ok(f"Python {version_text}（推荐区间 3.11 ~ 3.12）")
    elif (version.major, version.minor) >= (3, 13):
        fail(f"Python {version_text} —— 版本过高！vosk / webrtcvad 尚无对应 wheel")
        info("请改用 Anaconda 的 Python 3.12 重建虚拟环境")
    else:
        warn(f"Python {version_text}（未在推荐区间内）")

    info(f"解释器：{sys.executable}")

    missing_required: list[str] = []
    from importlib.metadata import version as _pkg_version

    for module, package, purpose, required in DEPENDENCIES:
        try:
            __import__(module)
            # 优先问安装元数据：dotenv / pydub 这类包没有 __version__ 属性，
            # 用 getattr 取只会得到空字符串，版本列白白空着
            try:
                ver = _pkg_version(package)
            except Exception:
                ver = getattr(sys.modules[module], "__version__", "")
            suffix = f" {ver}" if ver else ""
            ok(f"{package:<16}{suffix:<11}{purpose}")
        except Exception:
            if required:
                missing_required.append(package)
                fail(f"{package:<16}{'缺失':<11}{purpose}")
            else:
                warn(f"{package:<16}{'未安装':<11}{purpose}（后续里程碑才需要）")

    if missing_required:
        info()
        info(f"补齐命令：.venv\\Scripts\\python.exe -m pip install {' '.join(missing_required)}")

    return not missing_required


# ══════════════════════════════════════════════════════════
#  第 2 步：配置与密钥
# ══════════════════════════════════════════════════════════

def check_config():
    step(2, 5, "配置与密钥")
    config = Config.load()

    if ENV_FILE.exists():
        ok(f".env 已就位：{ENV_FILE}")
    else:
        warn(f".env 不存在（{ENV_FILE}）")
        info("复制 .env.example 为 .env 并填入密钥即可")

    key = config.secrets.dashscope_api_key
    if key:
        ok(f"百炼 API Key 已配置（{mask_secret(key)}）")
    else:
        fail("百炼 API Key 未配置 —— M2 之后的所有云端能力都会失败")
        info("获取地址：https://bailian.console.aliyun.com/")

    if config.secrets.weather_api_key:
        ok("心知天气 Key 已配置")
    else:
        warn("心知天气 Key 未配置（M5 天气工具才需要，现在不影响）")

    info()
    info("当前模型配置：")
    thinking = "开" if config.model.enable_thinking else "关"
    info(f"  对话 LLM : {config.model.llm}（思考模式：{thinking}）")
    info(f"  语音识别 : {config.model.asr}")
    info(f"  语音合成 : {config.model.tts} / 音色 {config.model.tts_voice}")
    info(f"  视觉理解 : {config.model.vlm}")
    info(
        f"  音频参数 : {config.audio.sample_rate}Hz / {config.audio.channels}声道"
        f" / {config.audio.sample_width * 8}bit（帧长 {config.audio.frame_ms}ms）"
    )
    return config


# ══════════════════════════════════════════════════════════
#  第 3 步：音频设备
# ══════════════════════════════════════════════════════════

def check_devices(config, show_all: bool = False) -> tuple[bool, bool]:
    step(3, 5, "音频设备")
    from app.audio.devices import default_device

    try:
        devices = list_devices()
    except Exception as exc:
        fail(f"设备枚举失败：{exc}")
        return False, False

    primary = [d for d in devices if d.hostapi == PREFERRED_HOSTAPI]
    hidden = len(devices) - len(primary)
    shown = devices if show_all else primary

    inputs = [d for d in shown if d.is_input]
    outputs = [d for d in shown if d.is_output]
    default_in = default_device(want_input=True)
    default_out = default_device(want_input=False)

    print(f"  🎤 输入设备（麦克风）· {len(inputs)} 个")
    for dev in inputs:
        marks = []
        if default_in and dev.index == default_in.index:
            marks.append("系统默认")
        if is_handsfree(dev.name):
            marks.append("免提模式·音质差")
        suffix = f"   ← {'，'.join(marks)}" if marks else ""
        print(f"     [{dev.index:>2}] {dev.name}{suffix}")

    print()
    print(f"  🔊 输出设备（扬声器 / 耳机）· {len(outputs)} 个")
    for dev in outputs:
        marks = []
        if default_out and dev.index == default_out.index:
            marks.append("系统默认")
        if is_handsfree(dev.name):
            marks.append("免提模式·音质差")
        suffix = f"   ← {'，'.join(marks)}" if marks else ""
        print(f"     [{dev.index:>2}] {dev.name}{suffix}")

    if not show_all and hidden > 0:
        print()
        info(f"另有 {hidden} 个端点属于其它驱动接口（DirectSound / WASAPI / WDM-KS），")
        info("同一硬件在不同接口下会重复出现，已折叠。加 --all-devices 可查看全部。")

    # ---------- 推荐路由 ----------
    # 路由知识来自 app/audio/devices.py —— 与 main.py --devices 共用同一实现，
    # 将来编排层做设备自动选择时也走这里
    route = suggest_route(devices)
    print()
    print("  💡 推荐路由")
    if route.input_device:
        info(f"麦克风 → {route.input_device.name}（index {route.input_device.index}）")
    if route.output_device:
        info(f"播放   → {route.output_device.name}（index {route.output_device.index}）")
    info(f"理由：{route.reason}")
    if config.audio.input_device or config.audio.output_device:
        info()
        info(f"当前 .env 已指定：输入='{config.audio.input_device}' 输出='{config.audio.output_device}'")
    else:
        info()
        info("如需固定该路由，在 .env 中设置 AYUAN_INPUT_DEVICE / AYUAN_OUTPUT_DEVICE")

    return bool([d for d in devices if d.is_input]), bool([d for d in devices if d.is_output])


# ══════════════════════════════════════════════════════════
#  第 4 步：录音
# ══════════════════════════════════════════════════════════

def _draw_meter(level: float, width: int = 34) -> str:
    """把 0~1 的音量映射成一条彩色电平条。"""
    scaled = min(level / 0.25, 1.0)
    filled = int(scaled * width)
    bar = "█" * filled + "░" * (width - filled)
    if not _supports_color():
        return bar
    if scaled > 0.75:
        color = "\033[31m"
    elif scaled > 0.35:
        color = "\033[32m"
    else:
        color = "\033[33m"
    return f"{color}{bar}\033[0m"


def record_test(config, seconds: float) -> bytes | None:
    step(4, 5, "录音测试")
    from app.audio.capture import Microphone
    from app.core.errors import AudioDeviceError

    mic = Microphone(config.audio)
    info(f"麦克风：{mic.device_name()}")
    print()
    print(f"  🎤 请对着麦克风说话，持续 {seconds:.0f} 秒…")
    print()

    target_samples = int(seconds * config.audio.sample_rate)
    # 每帧的字节数 = 采样宽度 × 声道数，不能写死成 2（单声道假设）
    bytes_per_sample = config.audio.sample_width * config.audio.channels
    collected = 0
    chunks: list[bytes] = []
    peak = 0.0

    try:
        mic.open()
        for frame in mic.frames():
            chunks.append(frame)
            collected += len(frame) // bytes_per_sample

            level = Microphone.rms(frame)
            peak = max(peak, level)

            progress = min(collected / target_samples, 1.0)
            print(
                f"\r  {_draw_meter(level)}  {level:6.4f}   进度 {progress * 100:5.1f}%",
                end="",
                flush=True,
            )
            if collected >= target_samples:
                break
    except AudioDeviceError as exc:
        print()
        fail(str(exc))
        return None
    finally:
        mic.close()

    print()
    pcm = b"".join(chunks)
    duration = collected / config.audio.sample_rate
    print()
    ok(f"录制完成：{duration:.1f} 秒 / {len(pcm)} 字节")
    info(f"峰值音量：{peak:.4f}")

    if peak < 0.005:
        fail("几乎全是静音 —— 麦克风可能被禁用、被独占，或选错了设备")
        info("排查：Windows 设置 → 隐私和安全性 → 麦克风 → 允许桌面应用访问")
        return None
    if peak < 0.02:
        warn("声音偏小，可以适当调高麦克风音量")
    else:
        ok("麦克风工作正常")

    return pcm


# ══════════════════════════════════════════════════════════
#  第 5 步：回放
# ══════════════════════════════════════════════════════════

def playback_test(config, pcm: bytes | None) -> CheckResult:
    """验证输出链路。

    没有录音时（例如机器没有麦克风）改播一声提示音 —— 回放能力与麦克风
    无关，不该因为录音失败就一并放弃验证。
    """
    step(5, 5, "回放测试")
    from app.audio.player import Speaker

    speaker = Speaker(config.audio)
    info(f"扬声器：{speaker.device_name()}")
    print()

    if pcm:
        print("  🔊 正在播放刚才的录音…")
        label = "播放录音"
    else:
        print("  🔔 没有录音可放，改播提示音来验证输出链路…")
        label = "播放提示音"

    try:
        # play() 返回「是否完整播完」，不能只看「有没有抛异常」——
        # 否则被打断或音频为空时会静默失败，而自检照样打印「播放完成」
        completed = speaker.play(pcm) if pcm else (speaker.beep() or True)
    except Exception as exc:
        fail(f"{label}失败：{exc}")
        return False

    print()
    if not completed:
        warn(f"{label}被中途打断（是否误触了停止？）")
        return False
    ok(f"{label}完成")
    return True


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

def main() -> int:
    _force_utf8()

    parser = argparse.ArgumentParser(description="阿源 M1 环境自检")
    parser.add_argument("--no-record", action="store_true", help="跳过录音与回放（无麦克风时用）")
    parser.add_argument("--all-devices", action="store_true", help="显示全部驱动接口下的设备端点")
    parser.add_argument("--seconds", type=float, default=None, help="录音时长（秒）")
    args = parser.parse_args()

    banner("阿源 · M1 环境自检")
    print("  目标：确认这台机器能「录到声音」且「放得出声音」")

    runtime_ok = check_runtime()
    config = check_config()
    has_mic, has_speaker = check_devices(config, show_all=args.all_devices)

    recorded: bytes | None = None
    playback_result: CheckResult = None

    if args.no_record:
        step(4, 5, "录音测试")
        info("已通过 --no-record 跳过")
        step(5, 5, "回放测试")
        info("已通过 --no-record 跳过")
    elif not has_mic:
        step(4, 5, "录音测试")
        fail("没有找到任何输入设备，无法录音")
        # 输出链路与麦克风无关 —— 仍然播一声提示音验证
        playback_result = playback_test(config, None)
    else:
        # 用 is None 而不是 or：--seconds 0 是合法输入，不该被 falsy 换成默认值
        seconds = config.audio.record_seconds if args.seconds is None else args.seconds
        recorded = record_test(config, seconds)
        playback_result = playback_test(config, recorded)

    record_result: CheckResult = None if args.no_record else (recorded is not None)

    banner("自检结果")
    checks: list[tuple[str, CheckResult]] = [
        ("运行环境与依赖", runtime_ok),
        ("配置与密钥", config.secrets.has_dashscope),
        ("输入设备存在", has_mic),
        ("输出设备存在", has_speaker),
        ("录音可用", record_result),
        ("回放可用", playback_result),
    ]

    for name, result in checks:
        if result is None:
            print(f"  ⊘  {name}（已跳过）")
        elif result:
            ok(name)
        else:
            fail(name)

    failures = [name for name, result in checks if result is False]
    skipped = [name for name, result in checks if result is None]

    print()
    if not failures:
        if skipped:
            print("  🎉 已检查的项目全部通过！")
            print(f"     （{len(skipped)} 项因 --no-record 跳过，去掉该参数即可完整测试）")
        else:
            print("  🎉 环境自检全部通过！")
            print("     下一步：python tools/m2_chat.py 就能跟她对话了")
    else:
        print(f"  ⚠️  {len(failures)} 项未通过，按上面的提示处理后重新运行：")
        for name in failures:
            print(f"       · {name}")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n已取消。")
        sys.exit(130)
