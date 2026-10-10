"""L3 感知层：把「连续的音频流」变成「有意义的语音事件」。

三个组件，职责逐层收窄：
    WakeWordDetector  —— 从音频流里听出唤醒词（M3）
    VoiceActivityDetector —— 判断单帧是否是人声
    SpeechSegmenter   —— 把帧序列切成一个个完整的语音段

这一层只吃 16kHz / 单声道 / 16bit 的 PCM 帧，不认识云端、不认识模型。
"""

from app.perception.segmenter import SpeechSegment, SpeechSegmenter
from app.perception.vad import VoiceActivityDetector

__all__ = ["VoiceActivityDetector", "SpeechSegmenter", "SpeechSegment"]
