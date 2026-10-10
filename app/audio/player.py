"""扬声器播放，支持中途打断。

M5 之前「打断」走的是最朴素的一条路：调用 stop() 立刻停声。
真正的「边说边听」（barge-in）需要回声消除，见 README 的路线图。

播放接口对上层统一成「给我一段音频，放完再返回」，格式自动识别：
    · 裸 PCM（16bit）—— 自检录音回放
    · WAV 容器        —— 百炼 TTS 返回的音频
"""

from __future__ import annotations

import io
import threading
import time
import wave

import numpy as np
import sounddevice as sd

from app.audio.devices import resolve_device
from app.config import AudioSettings
from app.core.errors import AudioDeviceError
from app.core.logger import get_logger

log = get_logger(__name__)


def parse_audio(data: bytes, fallback_rate: int) -> tuple[bytes, int]:
    """把任意音频字节规整成 (16bit PCM, 采样率)。

    识别 WAV 容器（RIFF 头）并取其内部参数；否则按裸 PCM 处理。
    """
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data), "rb") as wf:
                if wf.getsampwidth() != 2:
                    raise AudioDeviceError(
                        "音频位深不支持",
                        hint=f"只支持 16bit，收到 {wf.getsampwidth() * 8}bit",
                    )
                return wf.readframes(wf.getnframes()), wf.getframerate()
        except wave.Error as exc:
            log.warning("WAV 解析失败，按裸 PCM 处理：%s", exc)
    return data, fallback_rate


class Speaker:
    """扬声器。"""

    def __init__(self, settings: AudioSettings) -> None:
        self.settings = settings
        self._device_index: int | None = None
        self._device_resolved = False
        self._stop_event = threading.Event()
        self._playing = threading.Event()
        self._lock = threading.Lock()

    # ---------- 设备 ----------

    @property
    def device_index(self) -> int | None:
        if not self._device_resolved:
            self._device_index = resolve_device(self.settings.output_device, want_input=False)
            self._device_resolved = True
        return self._device_index

    def device_name(self) -> str:
        index = self.device_index
        if index is None:
            try:
                index = sd.default.device[1]
            except Exception:
                return "（未知）"
        if index is None or index < 0:
            return "（无可用输出设备）"
        try:
            return str(sd.query_devices(index)["name"])
        except Exception:
            return f"设备 #{index}"

    # ---------- 状态 ----------

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set()

    def stop(self) -> None:
        """请求停止播放。会在下一个音频块边界生效（约 20ms 内）。"""
        if self._playing.is_set():
            self._stop_event.set()
            log.debug("收到停止播放请求")

    # ---------- 播放 ----------

    def play(self, audio: bytes, *, sample_rate: int | None = None) -> None:
        """播放一段音频，阻塞直到播完或被 stop() 打断。"""
        if not audio:
            return

        rate = sample_rate or self.settings.sample_rate
        pcm, rate = parse_audio(audio, rate)

        with self._lock:
            self._stop_event.clear()
            self._playing.set()
            try:
                self._play_loop(pcm, rate)
            finally:
                self._playing.clear()

    def _play_loop(self, pcm: bytes, rate: int) -> None:
        # 按 20ms 分块写出：块越小，stop() 响应越快
        block_bytes = int(rate * self.settings.frame_ms / 1000) * self.settings.sample_width
        block_bytes = max(block_bytes, 320)

        try:
            stream = sd.OutputStream(
                device=self.device_index,
                samplerate=rate,
                channels=self.settings.channels,
                dtype="int16",
            )
        except Exception as exc:
            raise AudioDeviceError(
                "无法打开扬声器",
                hint=f"{self.device_name()} 打开失败：{exc}",
            ) from exc

        with stream:
            for offset in range(0, len(pcm), block_bytes):
                if self._stop_event.is_set():
                    log.info("播放已中断")
                    break
                chunk = pcm[offset : offset + block_bytes]
                if len(chunk) < self.settings.sample_width:
                    break
                frames = np.frombuffer(chunk, dtype=np.int16)
                stream.write(frames.reshape(-1, self.settings.channels))
            else:
                # for-else：没被打断，等缓冲区真正播完再返回
                time.sleep(0.05)

    def play_blocking_until_done(self, audio: bytes, *, sample_rate: int | None = None) -> bool:
        """播放并返回「是否完整播完」（False 表示被打断）。"""
        self.play(audio, sample_rate=sample_rate)
        return not self._stop_event.is_set()

    def beep(self, frequency: int = 880, duration: float = 0.12) -> None:
        """提示音。用于「唤醒成功」「录音开始」等状态反馈。"""
        rate = self.settings.sample_rate
        t = np.linspace(0, duration, int(rate * duration), endpoint=False)
        # 首尾各做 5ms 淡入淡出，避免爆音
        wave_data = np.sin(2 * np.pi * frequency * t) * 0.25
        fade = max(int(rate * 0.005), 1)
        envelope = np.ones_like(wave_data)
        envelope[:fade] = np.linspace(0, 1, fade)
        envelope[-fade:] = np.linspace(1, 0, fade)
        pcm = (wave_data * envelope * 32767).astype(np.int16).tobytes()
        self.play(pcm, sample_rate=rate)
