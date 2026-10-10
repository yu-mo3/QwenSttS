
r"""录音采样工具 —— 用于唤醒词验证。

为什么需要它：sherpa-onnx 的关键词唤醒模型**只认真人语音**，
对 TTS 合成音色不敏感（实测：同一句话，官方真人录音能唤醒，
TTS 合成的完全相同内容唤不醒）。所以唤醒词是否可用，
必须由本人录几段样本才能确认。

用法：
    .venv\Scripts\python.exe tools\record_sample.py
    .venv\Scripts\python.exe tools\record_sample.py --label ayuan --count 5
    .venv\Scripts\python.exe tools\record_sample.py --list

录好的样本存在 data/samples/ 下（该目录已被 .gitignore 忽略，
不会进入仓库，也不会离开这台电脑）。
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.audio.capture import Microphone
from app.config import Config
from app.core.errors import AudioDeviceError
from app.core.logger import ensure_utf8_stdout, setup_logging

SAMPLE_DIR = PROJECT_ROOT / "data" / "samples"

WIDTH = 62


def save_wav(path: Path, pcm: bytes, rate: int, channels: int = 1, width: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(width)
        f.setframerate(rate)
        f.writeframes(pcm)


def draw_meter(level: float, width: int = 30) -> str:
    scaled = min(level / 0.12, 1.0)
    filled = int(scaled * width)
    return "█" * filled + "░" * (width - filled)


def record_one(mic: Microphone, seconds: float, target_samples: int, bytes_per_sample: int):
    """录一段，返回 (pcm, 峰值)。"""
    chunks: list[bytes] = []
    collected = 0
    peak = 0.0

    mic.open()
    for frame in mic.frames():
        chunks.append(frame)
        collected += len(frame) // bytes_per_sample
        level = Microphone.rms(frame)
        peak = max(peak, level)
        print(f"\r  {draw_meter(level)}  {level:6.4f}", end="", flush=True)
        if collected >= target_samples:
            break
    print()
    return b"".join(chunks), peak


def main() -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="阿源 · 录音采样（唤醒词验证用）")
    parser.add_argument("--label", default="ayuan", help="样本标签，用作文件名前缀")
    parser.add_argument("--count", type=int, default=5, help="录几段")
    parser.add_argument("--seconds", type=float, default=2.0, help="每段时长")
    parser.add_argument("--phrase", default="阿源", help="提示用户说什么")
    parser.add_argument("--list", action="store_true", help="列出已有样本后退出")
    args = parser.parse_args()

    if args.list:
        files = sorted(SAMPLE_DIR.glob("*.wav"))
        if not files:
            print(f"  （{SAMPLE_DIR} 下还没有样本）")
            return 0
        print(f"  已有 {len(files)} 个样本：")
        for f in files:
            try:
                with wave.open(str(f), "rb") as w:
                    dur = w.getnframes() / w.getframerate()
                print(f"    {f.name:28} {dur:.2f}s")
            except Exception:
                print(f"    {f.name:28} （无法读取）")
        return 0

    config = Config.load()
    setup_logging("WARNING")
    mic = Microphone(config.audio)

    print()
    print("═" * WIDTH)
    print("  阿源 · 录音采样")
    print("═" * WIDTH)
    print(f"  麦克风：{mic.device_name()}")
    print(f"  计划录 {args.count} 段，每段 {args.seconds:.1f} 秒")
    print(f"  保存到：{SAMPLE_DIR}")
    print()
    print("  💡 建议每次用**略微不同的语气/语速**说，这样测出来的")
    print("     唤醒率才接近真实使用情况（而不是只测一种念法）")
    print()

    target = int(args.seconds * config.audio.sample_rate)
    bps = config.audio.sample_width * config.audio.channels
    saved: list[Path] = []
    empty = 0

    try:
        for i in range(1, args.count + 1):
            input(f"  [{i}/{args.count}] 准备好后按回车，然后说「{args.phrase}」…")
            print()
            pcm, peak = record_one(mic, args.seconds, target, bps)

            if peak < 0.005:
                empty += 1
                print(f"  ⚠️  第 {i} 段几乎是静音（峰值 {peak:.4f}），已跳过")
                continue

            path = SAMPLE_DIR / f"{args.label}_{i:02d}.wav"
            save_wav(path, pcm, config.audio.sample_rate,
                     config.audio.channels, config.audio.sample_width)
            saved.append(path)
            print(f"  ✅ 已保存 {path.name}（峰值 {peak:.4f}）")
            print()
    except (KeyboardInterrupt, EOFError):
        print("\n  已中断。")
    except AudioDeviceError as exc:
        print(f"\n  ❌ {exc}")
        return 1
    finally:
        mic.close()

    print()
    print("═" * WIDTH)
    if saved:
        print(f"  完成：{len(saved)} 段已保存到 {SAMPLE_DIR}")
        if empty:
            print(f"        （{empty} 段因静音被跳过）")
        print()
        print("  下一步：告诉协作方「样本已录好」，即可验证唤醒词")
    else:
        print("  没有录到有效样本。")
        print("  排查：Windows 设置 → 隐私和安全性 → 麦克风 → 允许桌面应用访问")
    print("═" * WIDTH)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
