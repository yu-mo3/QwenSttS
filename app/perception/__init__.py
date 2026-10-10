"""L3 感知层：把「连续的音频流」变成「有意义的语音事件」。

三个组件，职责逐层收窄：

    WakeWordDetector      —— 从音频流里听出唤醒词（KWS，离线）
    VoiceActivityDetector —— 判断单帧是否是人声（VAD + 能量双条件）
    SpeechSegmenter       —— 把帧序列切成一个个完整的语音段

这一层只吃 16kHz / 单声道 / 16bit 的 PCM 帧，不认识云端、不认识模型。

⚠️ 两个来自实测的使用纪律：
    1. VAD 与 KWS 的实例都是**有状态**的，每帧只能调一次；
       不要让两处逻辑共用同一实例。
    2. KWS **只认真人语音**，TTS 合成音唤不醒 —— 改关键词后必须真人复测。
"""

from app.perception.segmenter import SpeechSegment, SpeechSegmenter
from app.perception.vad import VadDecision, VoiceActivityDetector
from app.perception.wakeword import (
    ModelNotFoundError,
    WakeWordConfig,
    WakeWordDetector,
    default_config,
)

__all__ = [
    "VoiceActivityDetector",
    "VadDecision",
    "SpeechSegmenter",
    "SpeechSegment",
    "WakeWordDetector",
    "WakeWordConfig",
    "default_config",
    "ModelNotFoundError",
]
