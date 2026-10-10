"""L2 设备层：麦克风采集与扬声器播放。

对外只暴露三个东西：
    · list_devices()  枚举设备
    · Microphone      录音
    · Speaker         播放（可中断）

上层（唤醒、VAD、ASR、TTS）一律不直接碰 sounddevice。
"""

from app.audio.capture import Microphone
from app.audio.devices import AudioDevice, list_devices, resolve_device
from app.audio.player import Speaker

__all__ = ["Microphone", "Speaker", "AudioDevice", "list_devices", "resolve_device"]
