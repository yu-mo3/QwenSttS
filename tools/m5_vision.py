r"""M5 视觉验收 · 拍一张，让她说说看到了什么。

    .venv\Scripts\python.exe tools\m5_vision.py                 # 用摄像头拍
    .venv\Scripts\python.exe tools\m5_vision.py --image a.jpg   # 用现成的图
    .venv\Scripts\python.exe tools\m5_vision.py --save out.jpg  # 顺便存一份（调试用）
    .venv\Scripts\python.exe tools\m5_vision.py --text "看看这个零件有没有裂纹"

--image 这条路径存在的意义：**摄像头坏了也得能验证视觉链路**。
拍照与理解是两段，任何一段坏了都要能单独定位。

⚠️ 默认**不落盘**：拍到的可能是你的脸、家里、桌面。
--save 是显式的调试开关，不是默认行为。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.capabilities.vlm import VisionProvider  # noqa: E402
from app.camera import Camera  # noqa: E402
from app.config import Config  # noqa: E402
from app.core.errors import AyuanError  # noqa: E402
from app.core.logger import ensure_utf8_stdout, setup_logging  # noqa: E402
from app.orchestrator.vision_intent import (  # noqa: E402
    is_vision_request,
    strip_trigger,
    with_vision_context,
)

WIDTH = 64


def head(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def main() -> int:
    ensure_utf8_stdout()
    parser = argparse.ArgumentParser(prog="m5_vision", description="阿源 · 视觉理解验收")
    parser.add_argument("--image", type=Path, help="用现成的图片，不开摄像头")
    parser.add_argument("--save", type=Path, help="把拍到的图存一份（调试用；默认不落盘）")
    parser.add_argument("--text", default="看看这是什么", help="模拟用户说的话，用于验证触发判定")
    parser.add_argument("--prompt", default=None, help="覆盖给视觉模型的观察指令")
    parser.add_argument("--raw", action="store_true", help="跳过触发词判定，直接看图")
    args = parser.parse_args()

    config = Config.load()
    setup_logging("INFO")

    head("1/3 触发判定")
    print(f"  用户说：{args.text!r}")
    if args.raw:
        print("  --raw：跳过触发判定")
        note = args.text
    else:
        triggered = is_vision_request(args.text)
        note = strip_trigger(args.text)
        print(f"  判定结果：{'要看一眼' if triggered else '普通对话（本该直接聊天）'}")
        print(f"  补充说明：{note!r}")
        if not triggered:
            print()
            print("  判定为普通对话，不拍照。要强制看图请加 --raw")
            return 0

    head("2/3 取图")
    if args.image is not None:
        if not args.image.exists():
            print(f"  [失败] 找不到 {args.image}")
            return 1
        jpeg = args.image.read_bytes()
        print(f"  来源：文件 {args.image}（{len(jpeg) // 1024} KB）")
    else:
        camera = Camera(config.camera)
        try:
            started = time.monotonic()
            camera.open()
            print(f"  摄像头已打开（{time.monotonic() - started:.2f}s，索引 {config.camera.device_index}）")
            started = time.monotonic()
            jpeg = camera.grab_jpeg()
            print(f"  已抓图（{time.monotonic() - started:.2f}s，{len(jpeg) // 1024} KB，不落盘）")
        except AyuanError as exc:
            # 用 exc.message 而不是 str(exc)：后者会把 hint 也拼进去，
            # 于是同一句话打印两遍（KNOWN_ISSUES KI-009）。
            # 新代码不该把这个毛病复制下去。
            print(f"  [失败] {exc.message}")
            if exc.hint:
                print(f"         {exc.hint}")
            print()
            print("  摄像头这一段坏了，但视觉理解那一段还能单独验证：")
            print("    .venv\\Scripts\\python.exe tools\\check_camera.py        # 先诊断摄像头")
            print("    .venv\\Scripts\\python.exe tools\\m5_vision.py --image 图片.jpg")
            return 1
        finally:
            camera.close()

    if args.save is not None:
        args.save.write_bytes(jpeg)
        print(f"  已另存：{args.save}（仅因为你显式加了 --save）")

    head("3/3 交给视觉模型")
    provider = VisionProvider(config)
    print(f"  模型：{provider.model}")
    started = time.monotonic()
    try:
        result = provider.describe(jpeg, args.prompt)
    except AyuanError as exc:
        print(f"  [失败] {exc.message}")
        if exc.hint:
            print(f"         {exc.hint}")
        return 1
    elapsed = time.monotonic() - started

    print(f"  耗时：{elapsed:.2f}s")
    print()
    print("  她看到：")
    print(f"    {result.text}")
    if result.is_empty:
        print()
        print("  [警告] 模型返回了空描述")
        return 1

    if not args.raw and note:
        print()
        print("  交给对话模型的输入会是：")
        print(f"    {with_vision_context(result.text, note)}")

    print()
    print("=" * WIDTH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
