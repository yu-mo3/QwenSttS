"""M2 验收脚本 · 命令行语音对话。

    双击 run.bat 时的 M1 自检之外，这是 M2 的验收方式：

        .venv\\Scripts\\python.exe tools\\m2_chat.py

对着麦克风说一句话，阿源会用 Cherry 的声音回你。
输入 q 退出，输入 t 进入纯文字模式（不录音，直接打字测试对话）。

通过标准：
  1. 你说的话被准确识别成文字
  2. 她回的内容是人话（没有星号、表情符号、列表符号）
  3. 能听见她用语音把回复念出来
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config
from app.core.errors import ApiError, AudioDeviceError
from app.core.logger import ensure_utf8_stdout, get_logger, setup_logging
from app.orchestrator.pipeline import (
    EVT_ASR_DONE,
    EVT_ERROR,
    EVT_LLM_DONE,
    EVT_PLAYBACK_START,
    EVT_RECORDING_DONE,
    EVT_RECORDING_START,
    EVT_RETRY,
    EVT_TTS_DONE,
    VoicePipeline,
)

log = get_logger("m2")

WIDTH = 62


def head(title: str) -> None:
    print()
    print("═" * WIDTH)
    print(f"  {title}")
    print("═" * WIDTH)


def make_printer():
    """把 pipeline 的事件翻译成终端输出。"""
    state = {"sentence_index": 0, "total": 0, "first_byte": None}

    def on_event(event: str, payload: dict) -> None:
        if event == EVT_RECORDING_START:
            print(f"\n  🎤 请说话（{payload['seconds']:.0f} 秒）…")
        elif event == EVT_RECORDING_DONE:
            print(f"     录制完成 {payload['bytes']} 字节")
        elif event == EVT_ASR_DONE:
            text = payload["text"] or "（没听清）"
            emotion = f"  情绪：{payload['emotion']}" if payload.get("emotion") else ""
            print(f"  📝 你说：{text}{emotion}")
        elif event == EVT_LLM_DONE:
            state["total"] = len(payload["sentences"])
            fb = payload.get("first_byte")
            state["first_byte"] = fb
            fb_text = f"（首字 {fb:.2f}s）" if fb else ""
            print(f"  💬 阿源{fb_text}：{payload['text']}")
            if state["total"] > 1:
                print(f"     └ 拆成 {state['total']} 句播报")
        elif event == EVT_TTS_DONE:
            print(f"     🎵 合成第 {payload['index']}/{payload['total']} 句 · {payload['duration']:.1f}s 音频")
        elif event == EVT_PLAYBACK_START:
            print(f"     ▶️  播放第 {payload['index']} 句…")
        elif event == EVT_RETRY:
            print(f"     ⏳ 云端未响应，{payload['attempt']} 秒后重试（第 {payload['attempt']}/{payload['total']} 次）：{payload['reason'][:40]}")
        elif event == EVT_ERROR:
            print(f"\n  ❌ {payload['message']}")
            if payload.get("hint"):
                print(f"     {payload['hint']}")

    return on_event


# 已创建的流水线集合：退出时统一释放，避免音频设备被占住
_PIPELINES: list[VoicePipeline] = []


def build_pipeline(config: Config) -> VoicePipeline:
    pipeline = VoicePipeline(config, on_event=make_printer())
    _PIPELINES.append(pipeline)
    return pipeline


def pipeline_close() -> None:
    for pipeline in _PIPELINES:
        try:
            pipeline.close()
        except Exception:
            pass
    _PIPELINES.clear()


def text_mode(config: Config) -> int:
    """纯文字模式：跳过录音与识别，直接测试对话与合成。"""
    pipeline = build_pipeline(config)
    head("阿源 · 纯文字模式")
    print("  直接打字，她会用语音回你。输入 q 退出。")

    while True:
        try:
            user_text = input("\n  你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if user_text.lower() in {"q", "quit", "exit", "退出"}:
            break
        if not user_text:
            continue

        started = time.monotonic()
        result = pipeline.run_once(text_override=user_text)
        if result.failed:
            # 错误详情已由 EVT_ERROR 事件打印过，这里不再重复
            continue
        print(f"     ⏱  {result.timing_summary()}")

    print("\n  再见 👋")
    return 0


def voice_mode(config: Config, seconds: float) -> int:
    pipeline = build_pipeline(config)
    head("阿源 · 语音对话")
    print(f"  每次录音 {seconds:.0f} 秒。输入 q 后回车退出，输入 t 切换到纯文字模式。")
    print("  💡 提示：戴耳机效果最好（避免回声）")

    while True:
        try:
            command = input("\n  [回车] 开始说话   [t] 文字模式   [q] 退出  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if command in {"q", "quit", "exit", "退出"}:
            break
        if command == "t":
            return text_mode(config)

        try:
            result = pipeline.run_once(seconds)
        except AudioDeviceError as exc:
            print(f"\n  ❌ {exc}")
            return 1

        if result.failed:
            # 错误详情已由 EVT_ERROR 事件打印过，这里不再重复
            continue

        print(f"     ⏱  {result.timing_summary()}")
        if result.audio_seconds:
            print(f"     🔊 播报 {result.audio_seconds:.1f}s")

    print("\n  阿源已退出。")
    return 0


def main() -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="阿源 M2 验收 · 语音对话")
    parser.add_argument("--text", action="store_true", help="直接进入纯文字模式")
    parser.add_argument("--seconds", type=float, default=5.0, help="每次录音时长（默认 5 秒）")
    parser.add_argument("--say", type=str, default=None, help="只跑一轮，用给定文本当输入，跑完退出")
    args = parser.parse_args()

    config = Config.load()
    setup_logging(config.log_level)
    config.ensure_dirs()

    head("阿源 · M2 语音对话验收")
    print(f"  对话模型：{config.model.llm}")
    print(f"  识别模型：{config.model.asr}")
    print(f"  合成模型：{config.model.tts} · 音色 {config.model.tts_voice}")

    if not config.secrets.has_dashscope:
        print()
        print("  ❌ 未配置 DASHSCOPE_API_KEY，无法调用云端能力")
        print("     请在项目根目录的 .env 中填写后重试")
        return 1

    # 单轮非交互模式（供自动化验证使用）
    if args.say:
        pipeline = build_pipeline(config)
        result = pipeline.run_once(text_override=args.say)
        print(f"\n  ⏱  {result.timing_summary()}")
        return 1 if result.failed else 0

    try:
        if args.text:
            return text_mode(config)
        return voice_mode(config, args.seconds)
    finally:
        # 释放音频设备与 HTTP 连接池
        pipeline_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n已取消。")
        sys.exit(130)
