"""麦克风采集。

对外提供两种用法：
    record(seconds)  —— 定长录音，一次拿回完整 PCM（M1 自检、按住说话）
    frames()         —— 逐帧流式产出（M3 唤醒词与 VAD 需要 20ms 粒度）

为什么不直接用 sd.rec()：我们需要「按帧读取 + 溢出可见」，
定长阻塞接口在流式场景下会丢帧，而且溢出（overflow）会被静默吞掉。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import numpy as np
import sounddevice as sd

from app.audio.devices import resolve_device
from app.config import AudioSettings
from app.core.errors import AudioDeviceError
from app.core.logger import get_logger

log = get_logger(__name__)


class Microphone:
    """麦克风。设备在首次使用时才解析，避免 import 期就依赖硬件。"""

    def __init__(self, settings: AudioSettings) -> None:
        self.settings = settings
        self._device_index: int | None = None
        self._device_resolved = False
        self._stream: sd.InputStream | None = None

    # ---------- 设备 ----------

    @property
    def device_index(self) -> int | None:
        if not self._device_resolved:
            self._device_index = resolve_device(self.settings.input_device, want_input=True)
            self._device_resolved = True
        return self._device_index

    def device_name(self) -> str:
        index = self.device_index
        if index is None:
            try:
                index = sd.default.device[0]
            except Exception:
                return "（未知）"
        if index is None or index < 0:
            return "（无可用输入设备）"
        try:
            return str(sd.query_devices(index)["name"])
        except Exception:
            return f"设备 #{index}"

    # ---------- 流式 ----------

    def open(self) -> None:
        """打开输入流。重复调用是安全的。"""
        if self._stream is not None:
            return
        try:
            self._stream = sd.InputStream(
                device=self.device_index,
                samplerate=self.settings.sample_rate,
                channels=self.settings.channels,
                dtype="int16",
                blocksize=self.settings.frame_samples,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioDeviceError(
                "无法打开麦克风",
                hint=(
                    f"{self.device_name()} 打开失败：{exc}。"
                    "请确认设备未被其他程序独占（如会议软件、录音机），或改用系统默认设备。"
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

    def frames(self, *, timeout: float | None = None) -> Iterator[bytes]:
        """逐帧产出 20ms 的 PCM 数据（16bit 单声道）。

        timeout 为「连续无数据的容忍上限」，超时即结束迭代——
        用于「等用户开口，但不会永远等下去」的场景。
        """
        self.open()
        assert self._stream is not None
        deadline = None if timeout is None else time.monotonic() + timeout
        empty_reads = 0

        while True:
            try:
                data, overflowed = self._stream.read(self.settings.frame_samples)
            except Exception as exc:
                raise AudioDeviceError("读取麦克风数据失败", hint=str(exc)) from exc

            if overflowed:
                log.warning("音频输入溢出，已丢弃部分数据（通常是主线程被阻塞）")

            if data.size == 0:
                empty_reads += 1
                if deadline is not None and time.monotonic() > deadline:
                    log.debug("等待语音超时，结束采集")
                    return
                if empty_reads > 100:
                    return
                time.sleep(0.005)
                continue

            empty_reads = 0
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
        """
        seconds = self.settings.record_seconds if seconds is None else seconds
        total_frames = int(seconds * self.settings.sample_rate)
        collected = 0
        chunks: list[bytes] = []

        log.info("开始录音：%.1f 秒（%s）", seconds, self.device_name())

        with self:
            assert self._stream is not None
            while collected < total_frames:
                need = min(self.settings.frame_samples, total_frames - collected)
                try:
                    data, overflowed = self._stream.read(need)
                except Exception as exc:
                    raise AudioDeviceError("录音过程中断", hint=str(exc)) from exc
                if overflowed:
                    log.warning("音频输入溢出，已丢弃部分数据")
                if data.size == 0:
                    time.sleep(0.005)
                    continue
                chunks.append(np.asarray(data, dtype=np.int16).tobytes())
                collected += data.shape[0]
                if on_progress is not None:
                    on_progress(min(collected / total_frames, 1.0))

        pcm = b"".join(chunks)
        log.info("录音结束：%d 字节（%.1f 秒）", len(pcm), collected / self.settings.sample_rate)
        return pcm

    # ---------- 工具 ----------

    @staticmethod
    def pcm_to_float(pcm: bytes) -> np.ndarray:
        """PCM → [-1, 1] 浮点数组，便于计算能量。"""
        return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

    @staticmethod
    def rms(pcm: bytes) -> float:
        """均方根振幅（0~1）。静音约 0.000x，正常说话约 0.02~0.15。"""
        if not pcm:
            return 0.0
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples**2)) / 32768.0)
