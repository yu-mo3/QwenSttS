
r"""M3 验收脚本 · 免按钮语音对话。

喊一声「阿源」把她叫醒，然后直接说话，说完停顿一下她自己知道你说完了，
回答完继续听你说下一句 —— 不用反复喊唤醒词。

    .venv\Scripts\python.exe tools\m3_assistant.py
    .venv\Scripts\python.exe tools\m3_assistant.py --threshold 0.15
    .venv\Scripts\python.exe tools\m3_assistant.py --idle 20     # 静默 20 秒回待机

退出：说「再见」/「退下」，或按 Ctrl+C。

⚠️ 建议戴耳机。外放时麦克风会听到喇叭里的声音，可能被自己的播报内容
   误触发「停」。这是 M3 的已知限制，真正的「边说边听」需要回声消除。

通过标准：
  1. 喊「阿源」有反应（控制台出现「唤醒」）
  2. 说完话停顿一下，她自动开始处理（不用按键）
  3. 她用语音回答，且首句出声明显快于 M2
  4. 回答完继续说下一句，不需要再喊唤醒词
  5. 她说的时候喊「停」，她能停下来
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.core.errors import AyuanError  # noqa: E402
from app.core.logger import ensure_utf8_stdout, setup_logging  # noqa: E402
from app.orchestrator import assistant as ev  # noqa: E402
from app.orchestrator.assistant import VoiceAssistant  # noqa: E402

WIDTH = 62


def head(title: str) -> None:
    print()
    print("═" * WIDTH)
    print(f"  {title}")
    print("═" * WIDTH)


def make_printer() -> tuple:
    """把事件翻译成终端输出。返回 (回调, 计时盒)。"""
    box = {"wake_at": None, "speech_end_at": None, "first_audio_at": None}

    def on_event(event: str, payload: dict) -> None:
        now = time.monotonic()

        if event == ev.EVT_STATE:
            print(f"  ── 状态：{payload['old']} → {payload['new']}")
        elif event == ev.EVT_WAKE:
            box["wake_at"] = now
            print("\n  🔔 唤醒！请说话（说完停顿一下即可）")
        elif event == ev.EVT_SPEECH_END:
            box["speech_end_at"] = now
            print(f"  🎙️  说完了（{payload['duration']:.1f}s），正在识别…")
        elif event == ev.EVT_ASR_DONE:
            emo = f"  情绪：{payload['emotion']}" if payload.get("emotion") else ""
            text = payload["text"] or "（没听清）"
            print(f"  📝 你说：{text}{emo}")
        elif event == ev.EVT_LLM_DONE:
            fb = payload.get("first_byte")
            fb_text = f"（首字 {fb:.2f}s）" if fb else ""
            print(f"  💬 阿源{fb_text}：{payload['text']}")
        elif event == ev.EVT_TTS_DONE:
            if box["first_audio_at"] is None:
                box["first_audio_at"] = now
                if box["speech_end_at"]:
                    delay = now - box["speech_end_at"]
                    print(f"     ⏱️  说完到出声：{delay:.2f}s")
            print(f"     🎵 第 {payload['index']} 句 · {payload['duration']:.1f}s 音频")
        elif event == ev.EVT_PLAYBACK_START:
            print(f"     ▶️  播放第 {payload['index']} 句…")
        elif event == ev.EVT_INTERRUPTED:
            print("     🛑 被打断")
        elif event == ev.EVT_RETRY:
            print(f"     ⏳ 云端未响应，{payload['attempt']} 秒后重试"
                  f"（第 {payload['attempt']}/{payload['total']} 次）")
        elif event == ev.EVT_IDLE_TIMEOUT:
            print(f"\n  😴 静默 {payload['seconds']:.0f} 秒，回到待机（再说一次「阿源」）")
        elif event == ev.EVT_ERROR:
            print(f"\n  ❌ {payload['message']}")
            if payload.get("hint"):
                print(f"     {payload['hint']}")
        elif event == ev.EVT_EXIT:
            print(f"\n  👋 {payload.get('stats', '')}")

    return on_event, box


def main() -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="阿源 · M3 免按钮语音对话")
    parser.add_argument("--threshold", type=float, default=None, help="唤醒阈值（默认 0.2）")
    parser.add_argument("--idle", type=float, default=30.0, help="静默多少秒回待机")
    parser.add_argument("--turns", type=int, default=None, help="最多几轮对话后退出")
    args = parser.parse_args()

    config = Config.load()
    setup_logging(config.log_level)

    head("阿源 · M3 免按钮语音对话")
    print(f"  对话模型：{config.model.llm}")
    print(f"  唤醒阈值：{args.threshold if args.threshold is not None else 0.2}")

    if not config.secrets.has_dashscope:
        print("\n  ❌ 未配置 DASHSCOPE_API_KEY")
        return 1

    # 唤醒阈值可临时覆盖
    if args.threshold is not None:
        from app import perception
        from app.perception.wakeword import WakeWordConfig

        _orig = perception.WakeWordDetector

        class _Patched(_orig):  # type: ignore[misc,valid-type]
            def __init__(self, cfg=None):
                from app.perception.wakeword import default_config

                super().__init__(cfg or default_config(threshold=args.threshold))

        perception.WakeWordDetector = _Patched  # type: ignore[misc]

    on_event, _ = make_printer()
    assistant = VoiceAssistant(config, on_event=on_event, idle_timeout=args.idle)

    print()
    print("  💡 喊「阿源」唤醒；说「再见」或按 Ctrl+C 退出")
    print("  💡 建议戴耳机，避免喇叭声音被麦克风听到")
    print()

    try:
        stats = assistant.run(max_turns=args.turns)
    except AyuanError as exc:
        print(f"\n  ❌ {exc}")
        if exc.hint:
            print(f"     {exc.hint}")
        return 1
    except KeyboardInterrupt:
        print("\n  已中断。")
        return 0
    finally:
        assistant.close()

    print()
    print(f"  会话统计：{stats.summary()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
