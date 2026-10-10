"""麦克风采集。

对外提供两种用法：
    record(seconds)  —— 定长录音，一次拿回完整 PCM（自检、按住说话）
    frames()         —— 逐帧流式产出（唤醒词与 VAD 需要 20ms 粒度）

为什么不直接用 sd.rec()：我们需要「按帧读取 + 溢出可见」，
定长阻塞接口在流式场景下会丢帧，而且溢出（overflow）会被静默吞掉。

**两条路径都必须有终止条件。**
设备被独占、蓝牙耳机被系统切走、USB 拔插，都可能让 stream.read()
持续返回 0 帧而不抛异常 —— 没有上限就是永久挂死。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import numpy as np
import sounddevice as sd

from app.audio.devices import resolve_device, resolve_name
from app.config import AudioSettings
from app.core.errors import AudioDeviceError
from app.core.logger import get_logger

log = get_logger(__name__)

# 连续多少次「读到 0 帧」就认定设备已失效。
# 每帧间隔约 20ms，读空时我们 sleep 5ms，因此 100 次 ≈ 0.5 秒一个字节都没有。
_MAX_EMPTY_READS = 100

# 读空时让出 CPU 的时长
_EMPTY_READ_SLEEP = 0.005


class Microphone:
    """麦克风。设备在首次使用时才解析，避免 import 期就依赖硬件。"""

    def __init__(self, settings: AudioSettings) -> None:
        self.settings = settings
        self._device_index: int | None = None
        self._device_resolved = False
        self._stream: sd.InputStream | None = None
        self._overflow_count = 0

    # ---------- 设备 ----------

    @property
    def device_index(self) -> int | None:
        if not self._device_resolved:
            self._device_index = resolve_device(self.settings.input_device, want_input=True)
            self._device_resolved = True
        return self._device_index

    def device_name(self) -> str:
        return resolve_name(self.device_index, want_input=True)

    def _invalidate_device(self) -> None:
        """让设备索引在下次访问时重新解析。

        Windows 的设备索引会漂移：插上蓝牙耳机后 [#2 耳机] 可能变成 [#1]。
        长驻进程若一直用旧索引，会**静默打开另一台设备** —— 对着耳机说话
        却录进内置麦。所以在打开失败时主动失效，重解析一次再试。
        """
        self._device_resolved = False
        self._device_index = None

    # ---------- 流式 ----------

    def _create_stream(self) -> sd.InputStream:
        return sd.InputStream(
            device=self.device_index,
            samplerate=self.settings.sample_rate,
            channels=self.settings.channels,
            dtype="int16",
            blocksize=self.settings.frame_samples,
        )

    def open(self) -> None:
        """打开输入流。重复调用是安全的。"""
        if self._stream is not None:
            return

        try:
            self._stream = self._create_stream()
            self._stream.start()
        except Exception as first_error:
            # 索引可能已漂移 —— 重新解析一次再试，成功则说明确实是漂移
            self._invalidate_device()
            try:
                self._stream = self._create_stream()
                self._stream.start()
                log.warning("设备索引已漂移，重新解析后打开成功：%s", self.device_name())
            except Exception as exc:
                self._stream = None
                raise AudioDeviceError(
                    "无法打开麦克风",
                    hint=(
                        f"{self.device_name()} 打开失败：{exc}。"
                        "请确认设备未被其他程序独占（如会议软件、录音机），"
                        "或改用系统默认设备。首次错误：" + str(first_error)
                    ),
                ) from exc

        log.info("麦克风已打开：%s @ %dHz", self.device_name(), self.settings.sample_rate)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None

    def __enter__(self) -> "Microphone":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _read(self, frames: int) -> np.ndarray:
        """读一帧，把底层异常翻译成项目异常。"""
        assert self._stream is not None
        try:
            data, overflowed = self._stream.read(frames)
        except Exception as exc:
            raise AudioDeviceError("读取麦克风数据失败", hint=str(exc)) from exc
        if overflowed:
            # 限流：常驻助手用独立线程读帧、主线程在识别/播报时会短暂不消费，
            # 溢出属于预期现象。每次都警告会把终端刷满，反而淹没真正的问题。
            self._overflow_count += 1
            if self._overflow_count == 1:
                log.warning("音频输入溢出（后续同类警告将降为 debug）")
            else:
                log.debug("音频输入溢出 x%d", self._overflow_count)
        return np.asarray(data, dtype=np.int16)

    def frames(
        self,
        *,
        timeout: float | None = None,
        max_empty_reads: int = _MAX_EMPTY_READS,
    ) -> Iterator[bytes]:
        """逐帧产出 20ms 的 PCM 数据（16bit）。

        timeout 是**空闲超时**：连续这么久没有拿到任何数据就结束迭代。
        每次成功读到数据都会**重新计时** —— 所以「静默 9.9 秒后开口」
        不会被截断，只要两帧之间的间隔没超过上限。

        这正对应「等用户开口，但不会永远等下去」的语义。
        """
        self.open()
        empty_reads = 0
        idle_deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            data = self._read(self.settings.frame_samples)

            if data.size == 0:
                empty_reads += 1
                if idle_deadline is not None and time.monotonic() > idle_deadline:
                    log.debug("空闲超时（%.1fs 无数据），结束采集", timeout)
                    return
                if empty_reads > max_empty_reads:
                    log.warning("连续 %d 次读空，判定设备已失效，结束采集", empty_reads)
                    return
                time.sleep(_EMPTY_READ_SLEEP)
                continue

            # 拿到数据 → 空闲计时重新开始
            empty_reads = 0
            if timeout is not None:
                idle_deadline = time.monotonic() + timeout

            yield data.tobytes()

    # ---------- 定长 ----------

    def record(
        self,
        seconds: float | None = None,
        *,
        on_progress: Callable[[float], None] | None = None,
    ) -> bytes:
        """录音指定秒数，返回 16bit 单声道 PCM 字节串。

        on_progress 收到 0.0~1.0 的进度，用于在终端画进度条。

        Raises:
            AudioDeviceError: 设备打开失败，或持续读不到数据（已失效）。
                绝不无限循环 —— 用户总拿得回控制权。
        """
        seconds = self.settings.record_seconds if seconds is None else seconds
        total_frames = int(seconds * self.settings.sample_rate)
        collected = 0
        empty_reads = 0
        chunks: list[bytes] = []

        log.info("开始录音：%.1f 秒（%s）", seconds, self.device_name())

        with self:
            while collected < total_frames:
                need = min(self.settings.frame_samples, total_frames - collected)
                data = self._read(need)

                if data.size == 0:
                    empty_reads += 1
                    if empty_reads > _MAX_EMPTY_READS:
                        raise AudioDeviceError(
                            f"录音中断：连续 {_MAX_EMPTY_READS} 次没有从麦克风读到数据",
                            hint=(
                                f"{self.device_name()} 可能已被拔出、被其他程序独占，"
                                "或驱动异常。请检查设备后重试。"
                            ),
                        )
                    time.sleep(_EMPTY_READ_SLEEP)
                    continue

                empty_reads = 0
                chunks.append(data.tobytes())
                collected += data.shape[0]
                if on_progress is not None:
                    on_progress(min(collected / total_frames, 1.0))

        pcm = b"".join(chunks)
        log.info("录音结束：%d 字节（%.1f 秒）", len(pcm), collected / self.settings.sample_rate)
        return pcm

    # ---------- 工具 ----------

    @staticmethod
    def _to_samples(pcm: bytes) -> np.ndarray:
        """PCM 字节 → int16 采样数组。

        **丢弃不足一个采样点的尾巴**，而不是抛 ValueError。
        奇数字节在音频链路里非常常见（按字节切片、被截断的网络流、
        上游少读一帧），而 np.frombuffer 会直接抛原生 ValueError ——
        它不属于 AyuanError 家族，上层按 AudioDeviceError 写的容错兜不住。
        音频场景下丢 1 个采样点（1/16000 秒）完全无感。
        """
        usable = len(pcm) - (len(pcm) % 2)
        if usable <= 0:
            return np.empty(0, dtype=np.int16)
        return np.frombuffer(pcm[:usable], dtype=np.int16)

    @classmethod
    def pcm_to_float(cls, pcm: bytes) -> np.ndarray:
        """PCM → [-1, 1] 浮点数组，便于计算能量。"""
        return cls._to_samples(pcm).astype(np.float32) / 32768.0

    @classmethod
    def rms(cls, pcm: bytes) -> float:
        """均方根振幅（0~1）。静音约 0.000x，正常说话约 0.02~0.15。

        对任意字节长度都安全（空、奇数长度均返回 0 或截断后的值）。
        """
        samples = cls._to_samples(pcm).astype(np.float32)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples**2)) / 32768.0)
