"""常驻语音助手：状态机 + 连续对话 + 打断 + 边说边播。

与 VoicePipeline 的分工（两者都保留）：

    VoicePipeline  —— 一轮对话的**执行者**，无状态，可单独用（M2 的验收脚本用它）
    VoiceAssistant —— 一次会话的**持有者**，管状态流转、连续对话、打断、超时

## 线程模型（这是本模块最容易写错的地方）

    输入线程 ──读麦克风──> 帧队列 ──取帧──> 主线程（状态机）
                                              │
                                              └─ SPEAKING 时启动 ─> 播报线程

为什么输入要单独一个线程：ASR 与 LLM 调用是阻塞的（0.3~2 秒）。
如果主线程在识别时不读麦克风，sounddevice 的内部缓冲会堆积并溢出。
输入线程永远只做「读一帧、塞队列」，队列满时丢最旧的帧 —— 用户
在等待回复期间说的话本来也不重要。

## 打断

播报期间主线程仍在读帧，把它们喂给**独立的**「停」检测器。
检测到就调 speaker.stop()，播报线程会在下一个音频块边界退出。

⚠️ 唤醒检测 / 停检测 / VAD 必须是**三个独立实例** ——
webrtcvad 与 sherpa-onnx 的流都携带内部状态，共用会互相污染。

外放时的注意事项：麦克风可能听到喇叭里的声音。建议戴耳机，
否则可能被自己的播报内容触发。这是 M3 的已知限制，
真正的「边说边听」（barge-in）需要回声消除，放在后续里程碑。
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.audio.capture import Microphone
from app.audio.player import Speaker
from app.capabilities.asr import SpeechRecognizer
from app.capabilities.llm import ChatProvider, ConversationMemory
from app.capabilities.tts import SpeechSynthesizer
from app.config import Config
from app.core.errors import AyuanError
from app.core.logger import get_logger
from app.orchestrator.states import AssistantState, InvalidTransition, can_transition

log = get_logger(__name__)

# ── 事件名 ──
EVT_STATE = "state"
EVT_WAKE = "wake"
EVT_SPEECH_START = "speech_start"
EVT_SPEECH_END = "speech_end"
EVT_ASR_DONE = "asr_done"
EVT_LLM_DONE = "llm_done"
EVT_TTS_DONE = "tts_done"
EVT_PLAYBACK_START = "playback_start"
EVT_PLAYBACK_DONE = "playback_done"
EVT_INTERRUPTED = "interrupted"
EVT_RETRY = "retry"
EVT_IDLE_TIMEOUT = "idle_timeout"
EVT_ERROR = "error"
EVT_EXIT = "exit"

EventCallback = Callable[[str, dict[str, Any]], None]

# 说这些词结束连续对话
BYE_WORDS = ("再见", "拜拜", "退下", "结束", "不用了", "退出")

# 帧队列容量：约 2 秒的 20ms 帧。满了丢最旧的。
_FRAME_QUEUE_SIZE = 100


@dataclass
class SessionStats:
    """一次会话的统计。"""

    turns: int = 0
    wake_count: int = 0
    interrupted: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def duration(self) -> float:
        return time.monotonic() - self.started_at

    def summary(self) -> str:
        return (
            f"{self.duration:.0f}s · {self.turns} 轮对话 · "
            f"唤醒 {self.wake_count} 次 · 打断 {self.interrupted} 次 · 错误 {self.errors} 次"
        )


@dataclass
class TurnResult:
    """一轮对话的结果。"""

    user_text: str = ""
    reply_text: str = ""
    audio_seconds: float = 0.0
    played: int = 0
    interrupted: bool = False
    failed: bool = False
    error: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    def timing_summary(self) -> str:
        order = ["识别", "对话", "播报"]
        parts = [f"{k} {self.timings[k]:.2f}s" for k in order if k in self.timings]
        if parts:
            parts.append(f"合计 {sum(self.timings.values()):.2f}s")
        return " · ".join(parts)


class _SentenceSpeaker:
    """边说边播：播放第 i 句时，第 i+1 句已经在合成。

    两个线程：producer 消费句子迭代器并编号入队，worker 逐句合成后
    把音频放到 ready 队列。主线程从 ready 取音频播放。

    这样网络往返（实测单次约 2.4 秒）被播放时间覆盖，
    而不是串行累加 —— 这是 M2 遗留的最痛的一点。
    """

    def __init__(self, tts: SpeechSynthesizer, speaker: Speaker, emit: EventCallback) -> None:
        self.tts = tts
        self.speaker = speaker
        self._emit = emit

    def run(self, sentences: Iterator[str]) -> tuple[float, int, bool]:
        """返回 (音频总时长, 成功播放句数, 是否被打断)。"""
        pending: queue.Queue = queue.Queue()
        ready: queue.Queue = queue.Queue()
        stop = threading.Event()
        errors: list[BaseException] = []
        DONE = object()

        def producer() -> None:
            try:
                for index, sentence in enumerate(sentences, 1):
                    if stop.is_set():
                        break
                    pending.put((index, sentence))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                pending.put(DONE)

        def worker() -> None:
            while True:
                item = pending.get()
                if item is DONE:
                    break
                index, sentence = item
                try:
                    result = self.tts.synthesize(sentence)
                    ready.put((index, result))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    ready.put((index, None))
                    break
            ready.put(DONE)

        threading.Thread(target=producer, daemon=True, name="ayuan-tts-feed").start()
        threading.Thread(target=worker, daemon=True, name="ayuan-tts-work").start()

        total_audio = 0.0
        played = 0
        interrupted = False

        while True:
            item = ready.get()
            if item is DONE:
                break
            index, result = item
            if result is None:
                continue
            if result.is_empty:
                log.warning("第 %d 句合成结果为空，跳过", index)
                continue

            total_audio += result.duration
            played += 1
            self._emit(EVT_TTS_DONE, index=index, duration=result.duration)
            self._emit(EVT_PLAYBACK_START, index=index)

            completed = self.speaker.play(result.audio)
            self._emit(EVT_PLAYBACK_DONE, index=index, completed=completed)

            if not completed:
                interrupted = True
                stop.set()
                self._emit(EVT_INTERRUPTED)
                break

        stop.set()
        if errors:
            raise errors[0]
        return total_audio, played, interrupted


class VoiceAssistant:
    """常驻语音助手。"""

    def __init__(
        self,
        config: Config,
        *,
        on_event: EventCallback | None = None,
        idle_timeout: float = 30.0,
        record_after_wake: bool = False,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.idle_timeout = idle_timeout
        #: True 时唤醒后走「定长录音」，False 时走 VAD 自动断句
        self.record_after_wake = record_after_wake

        # ---- 设备层 ----
        self.microphone = Microphone(config.audio)
        self.speaker = Speaker(config.audio)

        # ---- 能力层 ----
        self.asr = SpeechRecognizer(config, on_retry=self._on_retry)
        self.llm = ChatProvider(config)
        self.tts = SpeechSynthesizer(config, on_retry=self._on_retry)
        self.memory = ConversationMemory()

        # ---- 感知层（三个独立实例，绝不共用）----
        self._wake = None
        self._stop = None
        self._vad = None
        self._segmenter = None

        # ---- 运行时状态 ----
        self.state = AssistantState.IDLE
        self.stats = SessionStats()
        self._running = False
        self._frames: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_SIZE)
        self._last_activity = time.monotonic()
        self._speaker_thread: threading.Thread | None = None
        self._speaker_error: BaseException | None = None

    # ══════════════════════════════════════════════════════
    #  感知层延迟构造（模型缺失时给出可操作提示）
    # ══════════════════════════════════════════════════════

    @property
    def wake(self):
        if self._wake is None:
            from app.perception.wakeword import WakeWordDetector

            self._wake = WakeWordDetector()
        return self._wake

    @property
    def stop_detector(self):
        """检测「停」。必须是独立实例 —— KWS 的流是有状态的。"""
        if self._stop is None:
            from app.perception.wakeword import WakeWordConfig, WakeWordDetector

            base = self.wake.cfg
            self._stop = WakeWordDetector(
                WakeWordConfig(
                    model_dir=base.model_dir,
                    keywords_file=base.model_dir.parent / "keywords_stop.txt",
                    threshold=base.threshold,
                )
            )
        return self._stop

    @property
    def vad(self):
        if self._vad is None:
            from app.perception.vad import VoiceActivityDetector

            self._vad = VoiceActivityDetector(
                sample_rate=self.config.audio.sample_rate,
                frame_ms=self.config.audio.frame_ms,
            )
        return self._vad

    @property
    def segmenter(self):
        if self._segmenter is None:
            from app.perception.segmenter import SpeechSegmenter

            self._segmenter = SpeechSegmenter(self.vad)
        return self._segmenter

    # ══════════════════════════════════════════════════════
    #  状态与事件
    # ══════════════════════════════════════════════════════

    def _set_state(self, new: AssistantState) -> None:
        if new == self.state:
            return
        if not can_transition(self.state, new):
            raise InvalidTransition(self.state, new)
        old, self.state = self.state, new
        self._last_activity = time.monotonic()
        log.debug("状态 %s -> %s", old, new)
        self._emit(EVT_STATE, old=str(old), new=str(new))

    def _emit(self, event: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event, payload)
        except Exception as exc:  # 订阅方出错不该拖垮主流程
            log.warning("事件回调 %s 抛错：%s", event, exc)

    def _on_retry(self, attempt: int, total: int, reason: str) -> None:
        self._emit(EVT_RETRY, attempt=attempt, total=total, reason=reason)

    def stop(self) -> None:
        """请求退出。可以从其他线程调用。"""
        self._running = False
        self.speaker.stop()

    # ══════════════════════════════════════════════════════
    #  输入线程
    # ══════════════════════════════════════════════════════

    def _reader_loop(self) -> None:
        """只做「读一帧、塞队列」。队列满时丢最旧的帧。

        丢弃最旧而不是最新：用户等待回复期间说的话不重要，
        但如果刚醒过来，最新的话才是要听的。
        """
        try:
            for frame in self.microphone.frames():
                if not self._running:
                    break
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    try:
                        self._frames.get_nowait()
                        self._frames.put_nowait(frame)
                    except queue.Empty:
                        pass
        except AyuanError as exc:
            log.error("麦克风读取中断：%s", exc)
            self._emit(EVT_ERROR, message=str(exc), hint=getattr(exc, "hint", ""))
            self._running = False

    # ══════════════════════════════════════════════════════
    #  主循环
    # ══════════════════════════════════════════════════════

    def run(self, *, max_turns: int | None = None) -> SessionStats:
        """启动助手，直到收到退出指令。"""
        self._running = True
        self._set_state(AssistantState.IDLE)
        self.microphone.open()

        reader = threading.Thread(target=self._reader_loop, daemon=True, name="ayuan-input")
        reader.start()
        log.info("阿源已启动，等待唤醒词")

        try:
            while self._running:
                try:
                    frame = self._frames.get(timeout=0.2)
                except queue.Empty:
                    self._check_idle_timeout()
                    continue
                self._tick(frame)
                if max_turns is not None and self.stats.turns >= max_turns:
                    log.info("已达对话轮数上限 %d，退出", max_turns)
                    break
        except KeyboardInterrupt:
            log.info("收到中断信号")
        finally:
            self._running = False
            self.speaker.stop()
            self.microphone.close()
            self._set_state(AssistantState.EXITING)
            self._emit(EVT_EXIT, stats=self.stats.summary())
        return self.stats

    def _check_idle_timeout(self) -> None:
        """聆听状态下静默过久就回到待机。"""
        if self.state != AssistantState.LISTENING:
            return
        silent = time.monotonic() - self._last_activity
        if silent >= self.idle_timeout:
            log.info("静默 %.0f 秒，回到待机", silent)
            self._emit(EVT_IDLE_TIMEOUT, seconds=silent)
            self.segmenter.reset()
            self._set_state(AssistantState.IDLE)

    def _tick(self, frame: bytes) -> None:
        """处理一帧。按当前状态决定这帧用来做什么。"""
        state = self.state

        if state == AssistantState.IDLE:
            if self.wake.feed(frame):
                self._emit(EVT_WAKE)
                self.stats.wake_count += 1
                self.wake.reset()
                self.segmenter.reset()
                self._set_state(AssistantState.LISTENING)

        elif state == AssistantState.LISTENING:
            segment = self.segmenter.feed(frame)
            if segment is not None:
                self._emit(EVT_SPEECH_END, duration=segment.duration)
                self._run_turn(segment)

        elif state == AssistantState.SPEAKING:
            # 播报期间监听「停」
            if self.stop_detector.feed(frame):
                self.stop_detector.reset()
                log.info("收到「停」指令")
                self.speaker.stop()
                self.stats.interrupted += 1

        # THINKING 状态：主线程正在 ASR/LLM 里，帧由输入线程自行丢弃

    # ══════════════════════════════════════════════════════
    #  一轮对话
    # ══════════════════════════════════════════════════════

    def _run_turn(self, segment) -> TurnResult:
        result = TurnResult()
        started = time.monotonic()

        try:
            # ---- 识别 ----
            self._set_state(AssistantState.THINKING)
            t0 = time.monotonic()
            transcript = self.asr.transcribe(segment.pcm, segment.sample_rate)
            result.timings["识别"] = time.monotonic() - t0
            result.user_text = transcript.text
            self._emit(
                EVT_ASR_DONE,
                text=transcript.text,
                emotion=transcript.emotion,
                elapsed=result.timings["识别"],
            )

            if transcript.is_empty:
                log.info("没听清，回到待机")
                self._set_state(AssistantState.IDLE)
                return result

            # ---- 结束语 ----
            if any(w in transcript.text for w in BYE_WORDS):
                result.reply_text = transcript.text
                self._set_state(AssistantState.IDLE)
                self._running = False
                return result

            # ---- 生成 ----
            t0 = time.monotonic()
            reply = self.llm.reply(transcript.text, self.memory)
            result.timings["对话"] = time.monotonic() - t0
            result.reply_text = reply.text
            self._emit(
                EVT_LLM_DONE,
                text=reply.text,
                sentences=reply.sentences,
                first_byte=reply.stats.first_byte if reply.stats else None,
            )

            if not reply.text:
                log.warning("模型没有返回内容")
                self._set_state(AssistantState.IDLE)
                return result

            # ---- 边说边播 ----
            t0 = time.monotonic()
            audio, played, interrupted = self._speak(reply.sentences)
            result.timings["播报"] = time.monotonic() - t0
            result.audio_seconds = audio
            result.played = played
            result.interrupted = interrupted

            self.stats.turns += 1

            # ---- 回到聆听（连续对话）----
            if not self._running:
                return result
            self.segmenter.reset()
            if interrupted:
                # 被打断说明用户想说别的，直接继续听
                self._set_state(AssistantState.LISTENING)
            else:
                self._set_state(AssistantState.LISTENING)
            self._last_activity = time.monotonic()

        except AyuanError as exc:
            result.failed = True
            result.error = str(exc)
            self.stats.errors += 1
            self._emit(EVT_ERROR, message=str(exc), hint=getattr(exc, "hint", ""))
            if self.state not in (AssistantState.EXITING,):
                self._set_state(AssistantState.IDLE)
        except Exception as exc:  # noqa: BLE001 — 最后一道防线
            result.failed = True
            result.error = f"{type(exc).__name__}: {exc}"
            self.stats.errors += 1
            log.exception("对话流程出现未预料的异常")
            self._emit(EVT_ERROR, message=result.error, hint="")
            if self.state not in (AssistantState.EXITING,):
                self._set_state(AssistantState.IDLE)

        result.timings["合计"] = time.monotonic() - started
        return result

    def _speak(self, sentences: list[str]) -> tuple[float, int, bool]:
        """边说边播，同时让主线程继续处理帧以检测「停」。"""
        if not sentences:
            return 0.0, 0, False

        speaker = _SentenceSpeaker(self.tts, self.speaker, self._emit)
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["result"] = speaker.run(iter(sentences))
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        thread = threading.Thread(target=work, daemon=True, name="ayuan-speak")
        self._speaker_thread = thread
        self._set_state(AssistantState.SPEAKING)
        thread.start()

        # 主线程不 join，而是继续消费帧 —— 这样「停」才能被听到。
        # 帧队列由 run() 的循环填充，这里只需等待播报线程结束。
        while thread.is_alive():
            thread.join(timeout=0.2)

        self._speaker_thread = None
        if "error" in box:
            raise box["error"]
        audio, played, interrupted = box.get("result", (0.0, 0, False))
        return audio, played, interrupted

    def close(self) -> None:
        self._running = False
        self.speaker.stop()
        self.microphone.close()
