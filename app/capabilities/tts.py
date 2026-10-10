"""语音合成能力（TTS）。

走百炼 **原生端点**：实测 OpenAI 兼容的 `/audio/speech` 返回 404，
该端点并不存在。

返回值是 **WAV 字节**（不是 mp3）。百炼返回一个带签名的临时 URL，
需要立刻下载 —— 链接有有效期（响应里带 expires_at）。

音色：默认 Cherry（芊悦），实测 20 个音色全部可用。
人设是「知心知性、阳光开朗」的女声，Cherry 最贴合。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.capabilities.base import (
    CallStats,
    HttpClient,
    call_with_retry,
    wav_duration,
)
from app.config import Config
from app.core.errors import ApiError
from app.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class SynthesisResult:
    """一次合成结果。"""

    audio: bytes
    duration: float = 0.0
    stats: CallStats | None = None

    @property
    def is_empty(self) -> bool:
        return len(self.audio) < 100


class SpeechSynthesizer:
    """语音合成。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.http = HttpClient(config)

    @property
    def voice(self) -> str:
        return self.config.model.tts_voice

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        model: str | None = None,
        instructions: str | None = None,
    ) -> SynthesisResult:
        """把文字合成语音。

        Args:
            text: 要朗读的文本。调用方应先经 llm.clean_for_speech 清理，
                  TTS 会把星号、井号这类符号原样念出来。
            voice: 音色覆盖，默认取配置（Cherry）
            model: 模型覆盖。传 instructions 时应使用 instruct 版本
            instructions: 自然语言语气指令，例如
                  「用温柔知心、语速稍慢的语气，像老朋友聊天一样」
        """
        text = (text or "").strip()
        if not text:
            raise ApiError("没有内容可合成", hint="待合成的文本为空")

        model = model or self.config.model.tts
        voice = voice or self.voice

        payload: dict = {
            "model": model,
            "input": {"text": text, "voice": voice},
        }
        if instructions:
            payload["input"]["instructions"] = instructions

        started = time.monotonic()
        body = call_with_retry(
            lambda: self.http.post_json(
                f"{self.config.model.native_url}/services/aigc/multimodal-generation/generation",
                payload,
            ),
            what="语音合成",
        )

        url = self._extract_url(body)
        audio = call_with_retry(
            lambda: self.http.get_bytes(url),
            what="下载合成音频",
        )
        elapsed = time.monotonic() - started

        duration = wav_duration(audio)
        stats = CallStats(
            name="TTS",
            elapsed=elapsed,
            response_bytes=len(audio),
            extra={"文本": f"{len(text)}字", "音频": f"{duration:.1f}s", "音色": voice},
        )
        log.debug("合成完成：%s", stats.summary())

        return SynthesisResult(audio=audio, duration=duration, stats=stats)

    @staticmethod
    def _extract_url(body: dict) -> str:
        """从返回结构里取出音频下载地址。"""
        try:
            url = body["output"]["audio"]["url"]
        except (KeyError, TypeError) as exc:
            raise ApiError(
                "语音合成未返回音频地址",
                hint=f"返回结构不符合预期：{str(body)[:200]}",
            ) from exc

        if not url:
            raise ApiError("语音合成返回了空的音频地址")
        return str(url)
