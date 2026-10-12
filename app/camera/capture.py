"""摄像头采集。

## 与 app/audio/capture.py 的关系

**同构，但刻意更简单。** 音频那边有 record()（定长）与 frames()（流式）
两条路；摄像头只有一条：grab_jpeg() —— 一次「看看这是什么」只需要
一张能用的图，做流式没有用户价值，只会多出一堆要维护的状态。

## 从音频那边学来的三条纪律，这里一条都不少

1. **设备延迟解析** —— import 期不碰硬件，没摄像头也能 import 本模块
2. **读取必须有终止条件** —— 连续读到空帧到上限就报错，绝不无限重试
   （M1 审查 R1 的教训：麦克风读取没有上限会永久挂死）
3. **数据只在内存里流转** —— 返回 JPEG 字节，不落盘。
   拍到的可能是用户的脸、家里、屏幕，没有理由把它留在磁盘上

## 为什么不用 QtMultimedia

摄像头是 L2 设备层的设备，和麦克风同级。让它去 import PySide6
（那是 L6 表现层的工具箱）会直接破坏分层铁律。
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from app.config import CameraSettings
from app.core.errors import CameraError
from app.core.logger import get_logger

log = get_logger(__name__)

#: 读到空帧时让出 CPU 的时长。10ms 足够让摄像头驱动把下一帧准备好，
#: 又不会让「设备真的坏了」这种情况下白等太久（上限 10 次约 0.1 秒）。
_EMPTY_READ_SLEEP = 0.01

#: Windows 上优先用 DirectShow 后端：打开更快，也不会打一堆噪声警告。
#: 打不开时回落到默认后端 —— 某些采集卡/虚拟摄像头不认 DirectShow。
_PREFERRED_BACKEND = getattr(cv2, "CAP_DSHOW", None)


# ══════════════════════════════════════════════════════════
#  纯函数（可单测，不需要摄像头）
# ══════════════════════════════════════════════════════════

def scale_to_fit(frame: np.ndarray, max_edge: int) -> np.ndarray:
    """把帧缩放到长边不超过 max_edge。已经够小就**原样返回**。

    原样返回（而不是「缩放到刚好等于 max_edge」）是有意的：
    把小图放大再送去识别，只会让请求变大，没有任何新信息。
    """
    if frame is None or frame.size == 0:
        raise CameraError("画面是空的", hint="摄像头没有返回有效图像")

    height, width = frame.shape[:2]
    longest = max(height, width)
    if max_edge <= 0 or longest <= max_edge:
        return frame

    ratio = max_edge / float(longest)
    # 至少留 1 像素：极端参数（max_edge=1）下 round 会算出 0 尺寸，
    # 而 cv2.resize 对 0 尺寸是抛异常 —— 那会让整条链路以裸异常收场
    new_size = (max(1, int(round(width * ratio))), max(1, int(round(height * ratio))))
    return cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)


def encode_jpeg(frame: np.ndarray, *, max_edge: int = 1024, quality: int = 85) -> bytes:
    """BGR 帧 -> JPEG 字节。

    先缩放再编码（顺序不能反：先编码再缩放得先解码回来，白费一次转换）。

    编码失败抛 CameraError 而不是让 cv2.error 穿透 —— 后者不属于
    AyuanError 家族，上层按 CameraError 写的容错兜不住。
    """
    resized = scale_to_fit(frame, max_edge)
    params = [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(int(quality), 100))]
    try:
        ok, buffer = cv2.imencode(".jpg", resized, params)
    except cv2.error as exc:
        raise CameraError("画面编码失败", hint=f"OpenCV 报错：{exc}") from exc

    if not ok or buffer is None:
        raise CameraError("画面编码失败", hint="摄像头返回的帧可能已损坏")
    return buffer.tobytes()


# ══════════════════════════════════════════════════════════
#  设备
# ══════════════════════════════════════════════════════════

class Camera:
    """摄像头。设备在首次使用时才打开，避免 import 期就依赖硬件。"""

    def __init__(self, settings: CameraSettings | None = None) -> None:
        self.settings = settings or CameraSettings()
        self._capture = None

    # ---------- 生命周期 ----------

    @property
    def is_open(self) -> bool:
        return self._capture is not None and self._capture.isOpened()

    def open(self) -> None:
        """打开摄像头。重复调用是安全的。"""
        if self.is_open:
            return

        index = self.settings.device_index
        capture = self._create(index, _PREFERRED_BACKEND)
        if capture is None:
            capture = self._create(index, None)

        if capture is None:
            raise CameraError(
                f"无法打开摄像头（索引 {index}）",
                hint=(
                    "请确认摄像头没有被其它程序占用（会议软件、相机应用），"
                    "或用 AYUAN_CAMERA_DEVICE 指定另一个索引"
                ),
            )

        self._capture = capture
        log.info("摄像头已打开：索引 %d", index)
        self._warm_up()

    @staticmethod
    def _create(index: int, backend: int | None):
        try:
            capture = (
                cv2.VideoCapture(index)
                if backend is None
                else cv2.VideoCapture(index, backend)
            )
        except cv2.error:
            return None
        return capture if capture.isOpened() else None

    def _warm_up(self) -> None:
        """丢掉开头的若干帧。

        刚打开的摄像头前几帧常常是黑的或严重过曝 —— 驱动还在自动曝光/
        白平衡的收敛过程里。不丢的话，用户拍到的第一张可能是一片黑。
        """
        capture = self._capture
        if capture is None:
            return
        for _ in range(max(0, self.settings.warmup_frames)):
            capture.read()

    def close(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:  # noqa: BLE001 — 释放失败不该往外抛
                pass
            finally:
                self._capture = None

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------- 取图 ----------

    def grab_bgr(self) -> np.ndarray:
        """抓一帧原始 BGR 图像。"""
        self.open()
        capture = self._capture
        if capture is None:
            raise CameraError("摄像头未打开", hint="请先调用 open()")

        empty_reads = 0
        while True:
            ok, frame = capture.read()
            if ok and frame is not None and frame.size:
                return frame

            empty_reads += 1
            if empty_reads > max(1, self.settings.max_empty_reads):
                raise CameraError(
                    f"摄像头连续 {empty_reads} 次没有返回画面",
                    hint=(
                        "设备可能已被拔出、被其它程序抢占，或驱动异常。"
                        "请检查摄像头后重试"
                    ),
                )
            time.sleep(_EMPTY_READ_SLEEP)

    def grab_jpeg(self) -> bytes:
        """抓一张图，返回 JPEG 字节。**不落盘。**"""
        frame = self.grab_bgr()
        jpeg = encode_jpeg(
            frame,
            max_edge=self.settings.max_edge,
            quality=self.settings.jpeg_quality,
        )
        log.debug(
            "已抓图：%d KB（原始 %dx%d -> 长边 %d，质量 %d）",
            len(jpeg) // 1024,
            frame.shape[1],
            frame.shape[0],
            self.settings.max_edge,
            self.settings.jpeg_quality,
        )
        return jpeg
