"""语音识别能力（ASR）。

走百炼 **原生端点**（不是 OpenAI 兼容端点）：
实测 `/compatible-mode/v1/audio/transcriptions` 返回 SSL 层错误，
而 `/api/v1/services/aigc/multimodal-generation/generation` 工作正常。

音频传递方式：**裸 PCM → 内存中包成 WAV → base64 data URI 直传**。
全程不落盘 —— 这是相对原教程（先存固定路径 mp3 再读回来）的关键改进，
既避免了「连说两句互相覆盖」，也不把用户语音留在磁盘上。
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.capabilities.base import (
    CallStats,
    HttpClient,
    call_with_retry,
    pcm_to_wav,
    wav_duration,
)
from app.config import Config
from app.core.errors import ApiError
from app.core.logger import get_logger

log = get_logger(__name__)

# 实测带日期后缀的版本可用；不带日期的 qwen3-asr-flash 免费额度已耗尽
DEFAULT_MODEL = "qwen3-asr-flash-2026-02-10"


@dataclass
class Transcript:
    """一次识别结果。"""

    text: str
    emotion: str = ""
    duration: float = 0.0
    stats: CallStats | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class SpeechRecognizer:
    """语音识别。"""

    def __init__(
        self,
        config: Config,
        *,
        on_retry: Callable[[int, int, str], None] | None = None,
    ) -> None:
        self.config = config
        self.http = HttpClient(config, on_retry=on_retry)

    @property
    def model(self) -> str:
        return self.config.model.asr

    def transcribe(
        self,
        pcm: bytes,
        sample_rate: int | None = None,
        *,
        language: str = "zh",
    ) -> Transcript:
        """把一段语音转成文字。

        Args:
            pcm: 16bit 单声道裸 PCM 字节
            sample_rate: 采样率，默认取配置里的 16000
            language: 语言提示，'zh' 中文 / 'en' 英文 / 'auto' 自动
        """
        if not pcm:
            raise ApiError("没有音频可识别", hint="录音为空，请确认麦克风工作正常")

        sample_rate = sample_rate or self.config.audio.sample_rate
        wav = pcm_to_wav(
            pcm,
            sample_rate,
            channels=self.config.audio.channels,
            sample_width=self.config.audio.sample_width,
        )
        duration = wav_duration(wav)
        encoded = base64.b64encode(wav).decode("ascii")
        data_uri = f"data:audio/wav;base64,{encoded}"

        payload: dict = {
            "model": self.model,
            "input": {
                "messages": [
                    {"role": "user", "content": [{"audio": data_uri}]},
                ]
            },
            "parameters": {"result_format": "message"},
        }
        if language and language != "auto":
            payload["parameters"]["asr_options"] = {"language": language}

        started = time.monotonic()
        body = call_with_retry(
            lambda: self.http.post_json(
                f"{self.config.model.native_url}/services/aigc/multimodal-generation/generation",
                payload,
            ),
            what="语音识别",
            on_retry=self.http.on_retry,
        )
        elapsed = time.monotonic() - started

        text, emotion = self._parse(body)

        stats = CallStats(
            name="ASR",
            elapsed=elapsed,
            request_bytes=len(data_uri),
            extra={"音频": f"{duration:.1f}s", "模型": self.model},
        )
        log.debug("识别完成：%s", stats.summary())

        return Transcript(text=text, emotion=emotion, duration=duration, stats=stats)

    @staticmethod
    def _parse(body: dict) -> tuple[str, str]:
        """从百炼返回结构里取出文本与情感。

        结构形如：
            {"output": {"choices": [{"message": {
                "content": [{"text": "..."}],
                "annotations": [{"emotion": "happy", "language": "zh"}]
            }}]}}
        """
        # 整个结构遍历都必须在 try 内。
        # 原先只有「取 message」那一行在 try 里，后面的 message.get 与
        # "".join 一旦遇到畸形结构就抛裸 AttributeError / TypeError 穿透到用户
        # —— 这正是 M1 修过的同一类坑（异常不属于 AyuanError 家族，上层兜不住）。
        try:
            message = body["output"]["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError(f"message 应为字典，实际是 {type(message).__name__}")

            content = message.get("content")
            if isinstance(content, list):
                # 用 or 而不是 get 的默认值：云端可能显式返回 null，
                # 那时 part.get("text", "") 会拿到 None，join 会炸
                text = "".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict)
                )
            elif isinstance(content, str):
                text = content
            else:
                text = ""

            annotations = message.get("annotations") or []
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
            raise ApiError(
                "无法解析语音识别结果",
                hint=f"返回结构不符合预期：{str(body)[:200]}",
            ) from exc

        emotion = ""
        for annotation in annotations:
            if isinstance(annotation, dict) and annotation.get("emotion"):
                emotion = str(annotation["emotion"])
                break

        return text.strip(), emotion
