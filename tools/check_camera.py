r"""摄像头自检。

    .venv\Scripts\python.exe tools\check_camera.py

为什么单独做一个工具：摄像头打不开的原因通常**不在代码里** ——
被别的程序占着、驱动异常、隐私开关关着、笔记本的物理遮挡没推开。
这个脚本把这几件事一次查清楚，而不是让用户对着一句
「无法打开摄像头」猜。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402

from app.camera import Camera  # noqa: E402
from app.config import Config  # noqa: E402
from app.core.errors import AyuanError  # noqa: E402
from app.core.logger import ensure_utf8_stdout, setup_logging  # noqa: E402

WIDTH = 64


def head(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def warn(msg: str) -> None:
    print(f"  [警告] {msg}")


def fail(msg: str) -> None:
    print(f"  [失败] {msg}")


def info(msg: str = "") -> None:
    print(f"         {msg}")


def main() -> int:
    ensure_utf8_stdout()
    setup_logging("WARNING")

    config = Config.load()
    settings = config.camera

    head("1/3 环境")
    print(f"  OpenCV     : {cv2.__version__}")
    print(f"  摄像头索引 : {settings.device_index}")
    print(f"  长边上限   : {settings.max_edge} px")
    print(f"  JPEG 质量  : {settings.jpeg_quality}")
    try:
        registry = cv2.videoio_registry
        names = [registry.getBackendName(b) for b in registry.getBackends()]
        print(f"  可用后端   : {', '.join(sorted(set(names)))}")
    except Exception:  # noqa: BLE001
        warn("取不到后端列表（OpenCV 版本差异，不影响后续检查）")

    head("2/3 逐个后端尝试打开")
    backends = [
        ("默认", None),
        ("DirectShow", getattr(cv2, "CAP_DSHOW", None)),
        ("MediaFoundation", getattr(cv2, "CAP_MSMF", None)),
    ]
    opened = None
    for name, code in backends:
        if code is None and name != "默认":
            continue
        try:
            capture = cv2.VideoCapture(settings.device_index) if code is None else cv2.VideoCapture(settings.device_index, code)
            is_open = capture.isOpened()
            got = False
            if is_open:
                ret, frame = capture.read()
                got = bool(ret and frame is not None and frame.size)
            capture.release()
        except Exception as exc:  # noqa: BLE001
            fail(f"{name}: {type(exc).__name__}")
            continue

        if got:
            ok(f"{name}: 能打开，也能读到画面")
            opened = name
        elif is_open:
            warn(f"{name}: 能打开，但读不到画面（可能被占用或驱动异常）")
        else:
            fail(f"{name}: 打不开")

    head("3/3 按真实流程抓一张")
    if opened is None:
        fail("没有一个后端能拿到画面 —— 摄像头在系统层就用不了")
        info()
        info("这通常不是代码问题。按下面的顺序排查：")
        info("  1. 摄像头被别的程序占着（会议软件、相机应用、直播工具）")
        info("     —— 全部关掉再试")
        info("  2. 笔记本有物理遮挡/开关（很多机型是 Fn + 摄像头键）")
        info("  3. Windows 隐私设置：设置 > 隐私和安全性 > 相机")
        info("     —— 要允许「桌面应用访问你的相机」")
        info("  4. 设备管理器里看摄像头有没有黄色感叹号，有就重装驱动")
        info("  5. 用系统自带的「相机」应用试一下：它都打不开就是系统问题")
        info()
        info("换一个摄像头索引可以试：在 .env 里设 AYUAN_CAMERA_DEVICE=1")
        return 1

    try:
        camera = Camera(settings)
        started = time.monotonic()
        camera.open()
        open_cost = time.monotonic() - started
        started = time.monotonic()
        jpeg = camera.grab_jpeg()
        grab_cost = time.monotonic() - started
        camera.close()
    except AyuanError as exc:
        fail(exc.message)   # 不用 str(exc)：它会把 hint 也拼进去，打印两遍
        if exc.hint:
            info(exc.hint)
        return 1
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")
        return 1

    ok(f"抓图成功：{len(jpeg) // 1024} KB（打开 {open_cost:.2f}s，抓取 {grab_cost:.2f}s）")

    import numpy as np

    array = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if array is not None:
        ok(f"解码尺寸 {array.shape[1]}x{array.shape[0]}，平均亮度 {array.mean():.1f}")
        if array.mean() < 5:
            warn("画面几乎是全黑 —— 镜头遮挡、曝光未收敛，或摄像头没真正出图")

    print()
    print("  摄像头可用。可以试：")
    print("    .venv\\Scripts\\python.exe tools\\m5_vision.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
