"""L4 能力层：ASR / LLM / TTS / VLM。

对外只暴露「业务语义」的类，不暴露 HTTP 细节。
编排层只跟这里打交道，不直接碰 requests 或 openai SDK。
"""

from app.capabilities.asr import SpeechRecognizer, Transcript
from app.capabilities.base import CallStats, HttpClient, call_with_retry, pcm_to_wav
from app.capabilities.llm import (
    SYSTEM_PROMPT,
    ChatProvider,
    ChatReply,
    ConversationMemory,
    clean_for_speech,
    split_sentence,
)
from app.capabilities.tts import SpeechSynthesizer, SynthesisResult

__all__ = [
    # 基建
    "HttpClient",
    "CallStats",
    "call_with_retry",
    "pcm_to_wav",
    # 能力
    "SpeechRecognizer",
    "Transcript",
    "ChatProvider",
    "ChatReply",
    "ConversationMemory",
    "SpeechSynthesizer",
    "SynthesisResult",
    # 文本处理
    "SYSTEM_PROMPT",
    "split_sentence",
    "clean_for_speech",
]
