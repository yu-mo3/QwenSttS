r"""下载唤醒模型。

模型体积约 31MB，**不入库**（见 .gitignore 的 models/ 规则），
所以新克隆仓库后需要跑一次这个脚本。

用法：
    .venvScriptspython.exe tools/download_models.py
    .venvScriptspython.exe tools/download_models.py --check   # 只检查是否就绪
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import MODELS_DIR  # noqa: E402
from app.core.logger import ensure_utf8_stdout, setup_logging  # noqa: E402

MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
ARCHIVE = MODEL_NAME + ".tar.bz2"
RELEASE_TAG = "kws-models"
REPO = "k2-fsa/sherpa-onnx"

# 关键词定义。token 写法是「声母+韵母」，
# 「源」的声母 y 必须写出来 —— 写成 ā uán 完全不触发（实测穷举过 10 种拆分）。
KEYWORDS = "ā y uán @阿源\n"


def already_ready() -> bool:
    d = MODELS_DIR / MODEL_NAME
    return (d / "tokens.txt").exists() and (d / "encoder-epoch-13-avg-2-chunk-16-left-64.onnx").exists()


def download() -> bool:
    archive = MODELS_DIR / ARCHIVE
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"  下载 {ARCHIVE}（约 31MB）…")
    try:
        subprocess.run(
            ["gh", "release", "download", RELEASE_TAG, "-R", REPO,
             "-p", ARCHIVE, "-D", str(MODELS_DIR), "--clobber"],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("  ❌ 未找到 gh（GitHub CLI）。")
        print("     要么安装 gh，要么手动下载：")
        print(f"     https://github.com/{REPO}/releases/tag/{RELEASE_TAG}")
        print(f"     把 {ARCHIVE} 放到 {MODELS_DIR} 后重跑本脚本。")
        return False
    except subprocess.CalledProcessError as exc:
        print(f"  ❌ 下载失败：{exc.stderr or exc}")
        return False

    print("  解压…")
    with tarfile.open(archive, "r:bz2") as tf:
        tf.extractall(MODELS_DIR, filter="data")
    archive.unlink(missing_ok=True)
    return True


def write_keywords() -> None:
    path = MODELS_DIR / "keywords_ayuan.txt"
    if path.exists():
        print(f"  关键词文件已存在，保留：{path.name}")
        return
    path.write_text(KEYWORDS, encoding="utf-8")
    print(f"  写入关键词：{path.name} -> {KEYWORDS.strip()}")


def main() -> int:
    ensure_utf8_stdout()
    setup_logging("WARNING")

    parser = argparse.ArgumentParser(description="阿源 · 下载唤醒模型")
    parser.add_argument("--check", action="store_true", help="只检查是否就绪")
    args = parser.parse_args()

    print()
    print("═" * 58)
    print("  阿源 · 唤醒模型")
    print("═" * 58)

    if already_ready():
        print(f"  ✅ 模型就绪：{MODELS_DIR / MODEL_NAME}")
        write_keywords()
        return 0

    if args.check:
        print("  ⚠️  模型未就绪，运行不带 --check 的命令来下载")
        return 1

    if not download():
        return 1

    write_keywords()
    print()
    print(f"  ✅ 完成：{MODELS_DIR / MODEL_NAME}")
    print("     下一步：python tools/wake_test.py  用真人语音验证唤醒")
    print("═" * 58)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        sys.exit(130)
