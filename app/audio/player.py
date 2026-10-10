"""扬声器播放，支持中途打断。

播放接口对上层统一成「给我一段音频，放完再返回」，格式自动识别：
    · WAV 容器     —— 百炼 TTS 返回的音频
    · 裸 PCM(16bit) —— 自检录音回放

格式解析全部委托给 app/audio/wav.py —— 那里处理了「流式 WAV 假 size」
这个坑，本模块不重复实现。

「打断」目前走最朴素的一条路：stop() 立刻停声。
真正的「边说边听」（barge-in）需要回声消除，见 README 路线图。
"""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd

from app.audio.devices import resolve_device, resolve_name
from app.audio.wav import parse_wav
from app.config import AudioSettings
from app.core.errors import AudioDeviceError
from app.core.logger import get_logger

log = get_logger(__name__)

# 播放分块的最大时长（毫秒）。块越小 stop() 响应越快，但系统调用越频繁。
# 20ms 是实测下来「响应速度」与「CPU 开销」的平衡点。
_MAX_BLOCK_MS = 20


def parse_audio(data: bytes, fallback_rate: int) -> tuple[bytes, int, int]:
    """把任意音频字节规整成 (16bit PCM, 采样率, 声道数)。

    WAV 走 wav.parse_wav（不受假 size 影响）；其余按裸 PCM 处理。
    """
    info = parse_wav(data)
    if info is not None:
        return info.pcm, info.sample_rate, info.channels

    if data[:4] == b"RIFF":
        log.warning("看起来是 WAV 但解析失败，按裸 PCM 处理（音频可能已损坏）")
    return data, fallback_rate, 1


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
        return resolve_name(self.device_index, want_input=False)

    def _invalidate_device(self) -> None:
        """让设备索引在下次访问时重新解析。

        同 capture.py：Windows 的设备索引会漂移，长驻进程用旧索引会
        静默打开另一台设备（以为在放给耳机听，其实从扬声器外放）。
        """
        self._device_resolved = False
        self._device_index = None

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

    def play(self, audio: bytes, *, sample_rate: int | None = None) -> bool:
        """播放一段音频，阻塞直到播完或被 stop() 打断。

        Returns:
            True 表示完整播完；False 表示被中途打断。
        """
        if not audio:
            return True

        pcm, rate, channels = parse_audio(audio, sample_rate or self.settings.sample_rate)
        if not pcm:
            log.warning("音频解析后为空，跳过播放")
            return True

        with self._lock:
            self._stop_event.clear()
            self._playing.set()
            try:
                self._play_loop(pcm, rate, channels)
                return not self._stop_event.is_set()
            finally:
                self._playing.clear()

    def _open_stream(self, rate: int, channels: int) -> sd.OutputStream:
        """打开输出流；失败时重新解析设备再试一次（索引可能已漂移）。"""

        def _make() -> sd.OutputStream:
            return sd.OutputStream(
                device=self.device_index,
                samplerate=rate,
                channels=channels,
                dtype="int16",
            )

        try:
            return _make()
        except Exception as first_error:
            self._invalidate_device()
            try:
                stream = _make()
                log.warning("输出设备索引已漂移，重新解析后打开成功：%s", self.device_name())
                return stream
            except Exception as exc:
                raise AudioDeviceError(
                    "无法打开扬声器",
                    hint=(
                        f"{self.device_name()} 打开失败：{exc}。"
                        "请确认设备未被其他程序独占。首次错误：" + str(first_error)
                    ),
                ) from exc

    def _play_loop(self, pcm: bytes, rate: int, channels: int) -> None:
        frame_size = self.settings.sample_width * channels
        block_bytes = max(int(rate * _MAX_BLOCK_MS / 1000) * frame_size, frame_size * 160)

        stream = self._open_stream(rate, channels)

        with stream:
            for offset in range(0, len(pcm), block_bytes):
                if self._stop_event.is_set():
                    log.info("播放已中断")
                    break
                chunk = pcm[offset : offset + block_bytes]
                # 丢弃不足一帧的尾巴，否则 reshape 会失败
                usable = len(chunk) - (len(chunk) % frame_size)
                if usable <= 0:
                    break
                frames = np.frombuffer(chunk[:usable], dtype=np.int16)
                stream.write(frames.reshape(-1, channels))
            else:
                # for-else：没被打断 → 等硬件缓冲真正播完再返回
                time.sleep(0.05)

    def beep(self, frequency: int = 880, duration: float = 0.12) -> None:
        """提示音。用于「唤醒成功」「录音开始」等状态反馈。"""
        rate = self.settings.sample_rate
        t = np.linspace(0, duration, int(rate * duration), endpoint=False)
        wave_data = np.sin(2 * np.pi * frequency * t) * 0.25
        # 首尾各做 5ms 淡入淡出，避免爆音。
        # 上界必须夹住数组长度的一半，否则 duration 极小时
        # np.linspace(0,1,80) 无法广播进长度 1 的数组（实测 ValueError）。
        fade = min(max(int(rate * 0.005), 1), len(wave_data) // 2)
        if fade <= 0:
            return
        envelope = np.ones_like(wave_data)
        envelope[:fade] = np.linspace(0, 1, fade)
        envelope[-fade:] = np.linspace(1, 0, fade)
        pcm = (wave_data * envelope * 32767).astype(np.int16).tobytes()
        self.play(pcm, sample_rate=rate)
