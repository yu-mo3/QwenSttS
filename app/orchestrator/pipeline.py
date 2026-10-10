"""L5 编排层 · 第一版：单轮语音对话流水线。

    🎤 录音 → 📝 识别 → 💬 对话 → 🔊 合成 → ▶️ 播放

设计要点：

1. **编排层是唯一知道「先后顺序」的地方**
   能力层彼此不感知（ASR 不知道有 LLM），设备层更不知道云端存在。
   想换掉任何一环，只动这里和 config。

2. **事件回调而不是硬编码输出**
   pipeline 通过 on_event 广播进度，自己不 print。
   这样 M4 的图形界面可以直接订阅同一套事件，不用改一行 pipeline 代码。

3. **分句合成而不是整段合成**
   一次合成一大段，TTS 会读得又平又快；按句合成能在句间留出自然停顿，
   听感更像人在说话。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.audio.capture import Microphone
from app.audio.player import Speaker
from app.capabilities.asr import SpeechRecognizer, Transcript
from app.capabilities.llm import ChatProvider, ConversationMemory
from app.capabilities.tts import SpeechSynthesizer
from app.config import Config
from app.core.errors import ApiError, AyuanError
from app.core.logger import get_logger

log = get_logger(__name__)

# 事件名常量 —— 用常量而不是字面量，避免订阅方拼错字符串
EVT_RECORDING_START = "recording_start"
EVT_RECORDING_DONE = "recording_done"
EVT_ASR_DONE = "asr_done"
EVT_LLM_DONE = "llm_done"
EVT_TTS_DONE = "tts_done"
EVT_PLAYBACK_START = "playback_start"
EVT_PLAYBACK_DONE = "playback_done"
EVT_RETRY = "retry"
EVT_ERROR = "error"

EventCallback = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnResult:
    """一轮对话的完整结果与耗时。"""

    user_text: str = ""
    reply_text: str = ""
    sentences: list[str] = field(default_factory=list)
    emotion: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    audio_seconds: float = 0.0
    failed: bool = False
    error: str = ""

    def timing_summary(self) -> str:
        if not self.timings:
            return ""
        order = ["识别", "对话", "合成", "播放"]
        parts = [f"{k} {self.timings[k]:.2f}s" for k in order if k in self.timings]
        total = sum(self.timings.values())
        parts.append(f"合计 {total:.2f}s")
        return " · ".join(parts)


class VoicePipeline:
    """一轮语音对话的编排。"""

    def __init__(
        self,
        config: Config,
        *,
        on_event: EventCallback | None = None,
        memory: ConversationMemory | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.memory = memory or ConversationMemory()

        # L2 设备层
        self.microphone = Microphone(config.audio)
        self.speaker = Speaker(config.audio)

        # L4 能力层 —— 全部延迟初始化，没配密钥时不会在构造期就炸
        self._asr: SpeechRecognizer | None = None
        self._llm: ChatProvider | None = None
        self._tts: SpeechSynthesizer | None = None

    # ---------- 延迟构造（也方便测试时替换实现） ----------

    @property
    def asr(self) -> SpeechRecognizer:
        if self._asr is None:
            self._asr = SpeechRecognizer(self.config, on_retry=self._on_retry)
        return self._asr

    @property
    def llm(self) -> ChatProvider:
        if self._llm is None:
            self._llm = ChatProvider(self.config)
        return self._llm

    @property
    def tts(self) -> SpeechSynthesizer:
        if self._tts is None:
            self._tts = SpeechSynthesizer(self.config, on_retry=self._on_retry)
        return self._tts

    # ---------- 事件 ----------

    def _on_retry(self, attempt: int, total: int, reason: str) -> None:
        """云端调用失败后、重试前的回调。

        必须让用户看见 —— 否则超时加退避会让终端静默好几分钟，
        用户只会以为程序死了然后强杀。
        """
        self._emit(EVT_RETRY, attempt=attempt, total=total, reason=reason)

    def _emit(self, event: str, **payload: Any) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, payload)
            except Exception as exc:  # 订阅方出错不该拖垮主流程
                log.warning("事件回调 %s 抛错：%s", event, exc)

    # ---------- 主流程 ----------

    def listen(self, seconds: float | None = None) -> Transcript:
        """录音并识别。"""
        seconds = seconds or self.config.audio.record_seconds

        self._emit(EVT_RECORDING_START, seconds=seconds)
        started = time.monotonic()
        pcm = self.microphone.record(seconds)
        self._emit(EVT_RECORDING_DONE, bytes=len(pcm))

        transcript = self.asr.transcribe(pcm)
        self._emit(
            EVT_ASR_DONE,
            text=transcript.text,
            emotion=transcript.emotion,
            elapsed=time.monotonic() - started,
        )
        return transcript

    def think(self, user_text: str) -> tuple[str, list[str]]:
        """把用户的话交给模型，拿回回复文本与分句。"""
        reply = self.llm.reply(user_text, self.memory)
        self._emit(
            EVT_LLM_DONE,
            text=reply.text,
            sentences=reply.sentences,
            first_byte=reply.stats.first_byte if reply.stats else None,
        )
        return reply.text, reply.sentences

    def speak(self, sentences: list[str]) -> tuple[float, float, float]:
        """逐句合成并播放。

        返回 (合成耗时, 播放耗时, 音频总时长)。
        合成与播放分开计时 —— 之前合并成一项目，而 timing_summary() 的
        显示键却是分开的，导致「对话 1.53s · 合计 8.94s」这种分项对不上
        合计的输出，恰好把最慢的一环藏了起来。
        """
        tts_time = 0.0
        play_time = 0.0
        total_audio = 0.0
        played = 0

        for index, sentence in enumerate(sentences, 1):
            started = time.monotonic()
            result = self.tts.synthesize(sentence)
            tts_time += time.monotonic() - started

            if result.is_empty:
                log.warning("第 %d 句合成结果为空，跳过", index)
                continue

            total_audio += result.duration
            played += 1
            self._emit(EVT_TTS_DONE, index=index, total=len(sentences), duration=result.duration)

            self._emit(EVT_PLAYBACK_START, index=index)
            started = time.monotonic()
            self.speaker.play(result.audio)
            play_time += time.monotonic() - started
            self._emit(EVT_PLAYBACK_DONE, index=index)

        # 有句子却一句都没播出来 = 静默失败。
        # 否则 run_once 会返回 failed=False，而 --say 的退出码是 0 ——
        # 用户什么都没听到，CI 却判定验收通过。
        if sentences and played == 0:
            raise ApiError(
                "语音合成为空",
                hint="云端返回了音频但内容为空，可能是文本里只有符号或表情",
            )

        return tts_time, play_time, total_audio

    def run_once(
        self,
        seconds: float | None = None,
        *,
        text_override: str | None = None,
        speak: bool = True,
    ) -> TurnResult:
        """执行一轮完整对话。

        Args:
            seconds: 录音时长
            text_override: 跳过录音与识别，直接用它当用户输入
                           （自动化测试与「纯文字模式」用）
            speak: 是否合成并播放回复
        """
        result = TurnResult()
        try:
            # ---- 1&2. 录音 + 识别 ----
            if text_override is None:
                t0 = time.monotonic()
                transcript = self.listen(seconds)
                result.timings["识别"] = time.monotonic() - t0
                result.user_text = transcript.text
                result.emotion = transcript.emotion
                if transcript.is_empty:
                    result.failed = True
                    result.error = "没有识别到有效语音"
                    return result
            else:
                result.user_text = text_override.strip()

            # ---- 3. 对话 ----
            t0 = time.monotonic()
            result.reply_text, result.sentences = self.think(result.user_text)
            result.timings["对话"] = time.monotonic() - t0

            if not result.reply_text:
                result.failed = True
                result.error = "模型没有返回内容"
                return result

            # ---- 4&5. 合成 + 播放 ----
            if speak:
                tts_time, play_time, audio_seconds = self.speak(result.sentences)
                result.timings["合成"] = tts_time
                result.timings["播放"] = play_time
                result.audio_seconds = audio_seconds

        except AyuanError as exc:
            # 必须捕获整个 AyuanError 家族而不只是 ApiError：
            # Speaker.play() 抛的 AudioDeviceError（蓝牙中途断连、被会议软件抢占）
            # 原先会直接穿透 run_once，把 traceback 甩给用户。
            result.failed = True
            result.error = str(exc)
            self._emit(EVT_ERROR, message=str(exc), hint=exc.hint)
        except Exception as exc:  # noqa: BLE001 — 最后一道防线
            # 任何未预料的异常也不该以 traceback 收场
            result.failed = True
            result.error = f"{type(exc).__name__}: {exc}"
            log.exception("对话流程出现未预料的异常")
            self._emit(EVT_ERROR, message=result.error, hint="")

        return result

    def close(self) -> None:
        self.microphone.close()
