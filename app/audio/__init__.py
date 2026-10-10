"""L2 设备层：麦克风采集、扬声器播放、设备选择与路由推荐。

对外只暴露这四类东西：
    · 设备：AudioDevice / list_devices / resolve_device / suggest_route
    · 录音：Microphone
    · 播放：Speaker
    · WAV：pcm_to_wav / parse_wav / wav_duration

上层（唤醒、VAD、ASR、TTS、编排）一律不直接碰 sounddevice。
"""

from app.audio.capture import Microphone
from app.audio.devices import (
    PREFERRED_HOSTAPI,
    AudioDevice,
    DeviceRoute,
    default_device,
    default_index,
    is_handsfree,
    list_devices,
    resolve_device,
    resolve_name,
    suggest_route,
)
from app.audio.player import Speaker
from app.audio.wav import WavInfo, parse_wav, pcm_duration, pcm_to_wav, wav_duration

__all__ = [
    # 设备
    "AudioDevice",
    "DeviceRoute",
    "PREFERRED_HOSTAPI",
    "list_devices",
    "resolve_device",
    "resolve_name",
    "default_device",
    "default_index",
    "suggest_route",
    "is_handsfree",
    # 采集与播放
    "Microphone",
    "Speaker",
    # WAV
    "WavInfo",
    "parse_wav",
    "pcm_to_wav",
    "pcm_duration",
    "wav_duration",
]
