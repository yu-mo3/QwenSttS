r"""唤醒词验证 —— 用真人录音测试「阿源」能不能唤醒。

**为什么必须用真人录音**：KWS 模型只认真人语音。实测同一句话，
真人录音能唤醒、TTS 合成的完全相同内容唤不醒（已用云端 ASR
交叉验证过 TTS 音频本身正常）。所以改关键词或调阈值后，
必须用真人样本复测。

用法：
    # 1) 先录音（如果还没有样本）
    .venvScriptspython.exe toolsecord_sample.py --label ayuan --count 5

    # 2) 验证
    .venvScriptspython.exe toolswake_test.py
    .venvScriptspython.exe toolswake_test.py --threshold 0.15
    .venvScriptspython.exe toolswake_test.py --scan      # 扫描最优阈值

样本放在 data/samples/*.wav（该目录已被 .gitignore 忽略）。
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402

from app.config import MODELS_DIR, Config  # noqa: E402
from app.core.errors import AyuanError  # noqa: E402
from app.core.logger import ensure_utf8_stdout, setup_logging  # noqa: E402
from app.perception.wakeword import (  # noqa: E402
    DEFAULT_MODEL_DIR,
    WakeWordConfig,
    WakeWordDetector,
)

SAMPLE_DIR = PROJECT_ROOT / "data" / "samples"
WIDTH = 62


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as f:
        rate, channels, width = f.getframerate(), f.getnchannels(), f.getsampwidth()
        raw = f.readframes(f.getnframes())
    if width != 2:
        raise AyuanError(f"{path.name} 不是 16bit WAV")
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        n = int(len(samples) * 16000 / rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, n),
                            np.arange(len(samples)), samples).astype(np.float32)
    return samples, 16000


def make_config(threshold: float) -> WakeWordConfig:
    return WakeWordConfig(
        model_dir=MODELS_DIR / DEFAULT_MODEL_DIR,
        keywords_file=MODELS_DIR / "keywords_ayuan.txt",
        threshold=threshold,
    )


def run_samples(cfg: WakeWordConfig, samples: list[tuple[str, np.ndarray]]) -> list[str]:
    """返回被唤醒的样本名列表。"""
    detector = WakeWordDetector(cfg)
    hits: list[str] = []
    frame = 320  # 20ms
    for name, x in samples:
        detector.reset()
        pcm = (x * 32767).astype(np.int16).tobytes()
        woke = False
        # 逐帧喂入，模拟真实流式场景（而不是整段一次性喂）
        for i in range(0, len(pcm) - frame * 2 + 1, frame * 2):
            if detector.feed(pcm[i : i + frame * 2]):
                woke = True
                break
        if woke:
            hits.append(name)
    return hits


def main() -> int:
    ensure_utf8_stdout()

    parser = argparse.ArgumentParser(description="阿源 · 唤醒词验证")
    parser.add_argument("--threshold", type=float, default=None, help="唤醒阈值")
    parser.add_argument("--scan", action="store_true", help="扫描阈值找最优值")
    parser.add_argument("--dir", type=Path, default=SAMPLE_DIR, help="样本目录")
    args = parser.parse_args()

    setup_logging("WARNING")
    config = Config.load()

    print()
    print("═" * WIDTH)
    print("  阿源 · 唤醒词验证")
    print("═" * WIDTH)

    files = sorted(args.dir.glob("*.wav"))
    if not files:
        print(f"  ❌ {args.dir} 下没有 .wav 样本")
        print()
        print("  先录几段：")
        print("    .venv\\Scripts\\python.exe tools\\record_sample.py --label ayuan --count 5")
        return 1

    samples: list[tuple[str, np.ndarray]] = []
    for f in files:
        try:
            x, _ = load_wav(f)
            samples.append((f.name, x))
        except AyuanError as exc:
            print(f"  ⚠️  跳过 {f.name}：{exc}")

    print(f"  样本 {len(samples)} 段，来自 {args.dir}")
    for name, x in samples:
        print(f"     {name:28} {len(x)/16000:5.2f}s  峰值 {np.max(np.abs(x)):.3f}")
    print()

    keywords = (MODELS_DIR / "keywords_ayuan.txt")
    print(f"  关键词定义：{keywords.read_text(encoding='utf-8').strip() if keywords.exists() else '（缺失）'}")
    print()

    try:
        if args.scan:
            print("  ── 阈值扫描 ──")
            best: list[tuple[float, int]] = []
            for th in (0.02, 0.05, 0.08, 0.1, 0.12, 0.15, 0.18, 0.2, 0.25, 0.3, 0.4):
                hits = run_samples(make_config(th), samples)
                rate = len(hits) / len(samples) * 100 if samples else 0
                mark = "✅" if hits else "  "
                print(f"     {mark} th={th:<5} 唤醒 {len(hits)}/{len(samples)}  （{rate:.0f}%）")
                if hits:
                    best.append((th, len(hits)))
            print()
            if best:
                top = max(best, key=lambda x: (x[1], x[0]))
                print(f"  建议阈值：{top[0]}（唤醒率最高，且在能触发的值里取最大以压低误触发）")
            else:
                print("  ⚠️  没有任何阈值能唤醒 —— 关键词 token 写法可能不对")
                print("     检查 models/keywords_ayuan.txt，参考 app/perception/wakeword.py 的说明")
        else:
            threshold = args.threshold if args.threshold is not None else 0.2
            print(f"  ── 阈值 {threshold} ──")
            hits = run_samples(make_config(threshold), samples)
            for name, _ in samples:
                print(f"     {'✅ 唤醒' if name in hits else '   未触发'}  {name}")
            print()
            rate = len(hits) / len(samples) * 100 if samples else 0
            if rate >= 80:
                print(f"  🎉 唤醒率 {rate:.0f}% —— 可用")
            elif rate > 0:
                print(f"  ⚠️  唤醒率 {rate:.0f}%（偏低），试试 --scan 找更合适的阈值")
            else:
                print(f"  ❌ 唤醒率 0% —— 关键词写法或阈值有问题，试试 --scan")
    except AyuanError as exc:
        print(f"  ❌ {exc}")
        if exc.hint:
            print(f"     {exc.hint}")
        return 1

    print("═" * WIDTH)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
