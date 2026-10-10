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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.audio.capture import Microphone
from app.audio.player import Speaker
from app.capabilities.asr import SpeechRecognizer
from app.capabilities.llm import ChatProvider, ConversationMemory
from app.capabilities.tts import SpeechSynthesizer
from app.config import Config
from app.core.errors import ApiError, AyuanError
from app.core.logger import get_logger
from app.orchestrator.events import (
    EVT_ASR_DONE,
    EVT_ERROR,
    EVT_EXIT,
    EVT_IDLE_TIMEOUT,
    EVT_INTERRUPTED,
    EVT_LLM_DONE,
    EVT_PLAYBACK_DONE,
    EVT_PLAYBACK_START,
    EVT_RETRY,
    EVT_SPEECH_END,
    EVT_SPEECH_START,
    EVT_STATE,
    EVT_TTS_DONE,
    EVT_WAKE,
    EventCallback,
    TurnResult,
)
from app.orchestrator.states import AssistantState, InvalidTransition, can_transition

log = get_logger(__name__)

# 事件名 / EventCallback / TurnResult 都住在 events.py —— 与 pipeline 共用同一份
# 契约（M4 修 KI-006）。这里 import 进来【再导出】，既有的
#   from app.orchestrator.assistant import EVT_STATE
# 用法（tools/m3_assistant.py 就在用）保持不变。

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
        # emit 的签名就是 EventCallback(事件名, 载荷字典)。
        # 原先这里传的是 VoiceAssistant._emit(事件名, **载荷)，声明与调用对不上，
        # 属于「类型撒谎」—— 正是 KI-006 那一类问题的温床。
        # 现在由 VoiceAssistant._event_sink 适配成标准签名。
        self._emit = emit

    def run(self, sentences: Sequence[str]) -> tuple[float, int, bool]:
        """返回 (音频总时长, 成功播放句数, 是否被打断)。

        入参是 Sequence 而不是 Iterator：需要 len() 来给 tts_done 事件带上
        total（KI-006 —— 界面靠它显示「第 i / N 句」，pipeline 一直有、
        assistant 原先漏了，统一订阅时就是必现的 KeyError）。
        """
        total = len(sentences)
        # 一段播报 = 一个停止闭锁的作用域：开始前清掉上一轮遗留的停止请求，
        # 否则「上次点的停」会把这次的第一句吃掉（KI-007②）
        self.speaker.clear_stop()
        had_sentences = False   # 空列表也是一种「没有句子」，先记下来
        pending: queue.Queue = queue.Queue()
        ready: queue.Queue = queue.Queue()
        stop = threading.Event()
        errors: list[BaseException] = []
        DONE = object()

        def producer() -> None:
            nonlocal had_sentences
            try:
                for index, sentence in enumerate(sentences, 1):
                    if stop.is_set():
                        break
                    had_sentences = True
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
            self._emit(EVT_TTS_DONE, {"index": index, "total": total, "duration": result.duration})
            self._emit(EVT_PLAYBACK_START, {"index": index})

            completed = self.speaker.play(result.audio)
            self._emit(EVT_PLAYBACK_DONE, {"index": index, "completed": completed})

            if not completed:
                interrupted = True
                stop.set()
                self._emit(EVT_INTERRUPTED, {})
                break

        stop.set()
        if errors:
            raise errors[0]

        # 有句子却一句都没播出来 = 静默失败。
        # 否则用户什么都没听到，而程序认为这一轮正常完成。
        if had_sentences and played == 0:
            raise ApiError(
                "语音合成为空",
                hint="云端返回了音频但内容为空，可能是文本里只有符号或表情",
            )
        return total_audio, played, interrupted


class VoiceAssistant:
    """常驻语音助手。"""

    def __init__(
        self,
        config: Config,
        *,
        on_event: EventCallback | None = None,
        idle_timeout: float = 30.0,
        wake_threshold: float | None = None,
        stop_threshold: float | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self.idle_timeout = idle_timeout
        # 阈值作为构造参数传入。
        # 曾经的做法是在 tools 里 monkeypatch 包的命名空间，但 VoiceAssistant
        # 用的是模块命名空间的引用，补丁打不上 —— 参数看着生效、实际是空操作，
        # 控制台还照常打印"唤醒阈值：0.15"，排查时极具误导性。
        self._wake_threshold = wake_threshold
        self._stop_threshold = stop_threshold

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
        # 播报的运行时状态。播报跑在独立线程里，主线程**不等待**它 ——
        # 否则就回不到主循环，「停」的检测永远不会发生。
        self._speak_thread: threading.Thread | None = None
        self._speak_box: dict[str, Any] = {}
        self._speak_result: TurnResult | None = None
        self._speak_started: float = 0.0

    # ══════════════════════════════════════════════════════
    #  感知层延迟构造（模型缺失时给出可操作提示）
    # ══════════════════════════════════════════════════════

    @property
    def wake(self):
        if self._wake is None:
            from app.perception.wakeword import WakeWordDetector, default_config

            self._wake = WakeWordDetector(default_config(threshold=self._wake_threshold))
        return self._wake

    @property
    def wake_threshold(self) -> float:
        """实际生效的唤醒阈值（供验收脚本回读打印，避免"看着生效其实没生效"）。"""
        return self.wake.cfg.threshold

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
                    threshold=(
                        self._stop_threshold
                        if self._stop_threshold is not None
                        else base.threshold
                    ),
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

    def _event_sink(self, event: str, payload: dict[str, Any]) -> None:
        """把标准 EventCallback 签名适配到内部的 _emit(事件名, **载荷)。

        内部保留 **载荷 的写法是因为绝大多数调用点只关心三两个键，
        key=value 比手写字面量字典好读；对外（_SentenceSpeaker）则统一走
        标准签名，避免同一个项目里出现两种事件回调约定。
        """
        self._emit(event, **payload)

    def _on_retry(self, attempt: int, total: int, reason: str) -> None:
        self._emit(EVT_RETRY, attempt=attempt, total=total, reason=reason)

    def stop(self) -> None:
        """请求退出。可以从其他线程调用。"""
        self._running = False
        self.speaker.stop()

    def _begin_new_session(self) -> None:
        """把会话级状态归位，为一次新的 run() 做准备（KI-007①）。

        刻意**绕过转换表**：EXITING -> IDLE 在同一会话内确实是非法转换
        （转换表因此拒绝它），但「上一个会话已结束，现在开一个新的」
        是合法的重置语义。写成一个具名方法而不是散在 run() 里，
        是为了让「这里故意绕过了状态机」这件事一眼可见、且只有一处。

        对话记忆**不清空** —— 关掉界面再打开，她应该还记得刚才聊了什么。
        """
        if self.state is AssistantState.EXITING:
            log.info("上一个会话已结束，开始新会话")
        self.state = AssistantState.IDLE
        self.stats = SessionStats()
        self._last_activity = time.monotonic()
        # 清掉上一轮遗留的停止闭锁，否则新会话的第一句会被静默吃掉
        self.speaker.clear_stop()
        # 队列里可能还压着上一个会话的音频帧，带进新会话会让它「听到」旧声音
        self._drain_frames()
        if self._segmenter is not None:
            self._segmenter.reset()

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
        except Exception as exc:  # noqa: BLE001 — 读线程的任何退出都要被主循环知道
            log.error("麦克风读取中断：%s", exc)
            self._emit(EVT_ERROR, message=str(exc), hint=getattr(exc, "hint", ""))
        finally:
            # 【重要】线程结束未必是异常。
            # capture.frames() 在连续读空达到上限后会**正常 return**，
            # for 循环随之正常结束 —— 这时主循环还在傻等，_running 仍为 True，
            # 于是助手永久挂死：不报错、不退出、唤不醒，只能杀进程。
            # 所以无论怎么结束，只要 _running 还是 True，就说明是设备侧没了。
            if self._running:
                log.error("麦克风输入已停止（设备被拔出、被独占或驱动异常）")
                self._emit(
                    EVT_ERROR,
                    message="麦克风输入意外停止",
                    hint="设备可能已被拔出或被其他程序占用，请检查后重启阿源",
                )
                self._running = False

    # ══════════════════════════════════════════════════════
    #  主循环
    # ══════════════════════════════════════════════════════

    def run(self, *, max_turns: int | None = None) -> SessionStats:
        """启动助手，直到收到退出指令。

        **可重入**（M4 修 KI-007①）：EXITING 是会话内的终态，但再次调用
        run() 的语义是「开一个新会话」，而不是非法转换。原实现直接把
        EXITING -> IDLE 喂给 _set_state，第二次 run() 必然抛
        InvalidTransition，且异常发生在 try 之外 —— 界面的「重新开始会话」
        一按就拿到 traceback。
        """
        if self._running:
            raise AyuanError(
                "助手已经在运行中",
                hint="同一个 VoiceAssistant 不能并发跑两个会话，请先 stop()",
            )

        # 会话级归位：不走转换表。EXITING -> IDLE 在**同一会话内**确实非法
        # （所以才不在 ALLOWED_TRANSITIONS 里），但跨会话是合法的重置。
        self._begin_new_session()

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
                # 静默判定必须每帧都查。
                # 原先只在队列 Empty 时查，而真实麦克风每 20ms 供一帧，
                # get() 永远能在超时前返回 —— 结果是「静默 30 秒回待机」
                # 从未生效，状态永远停在聆听。
                self._check_idle_timeout()
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
        """聆听状态下静默过久就回到待机。

        由主循环**每帧**调用（不只是队列空的时候）—— 见 run() 里的说明。
        """
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
            was_speaking = self.segmenter.is_speaking
            segment = self.segmenter.feed(frame)
            if not was_speaking and self.segmenter.is_speaking:
                # EVT_SPEECH_START 原先只定义、从不发出（死常量）。
                # 界面要它来区分「已唤醒但还没开口」和「正在说话」——
                # 后者才该让光球的涟漪跟着音量动。
                self._emit(EVT_SPEECH_START)
            if segment is not None:
                self._emit(EVT_SPEECH_END, duration=segment.duration)
                self._run_turn(segment)

        elif state == AssistantState.SPEAKING:
            self._tick_speaking(frame)

        # THINKING 状态：主线程正在 ASR/LLM 里，帧由输入线程自行丢弃

    # ══════════════════════════════════════════════════════
    #  一轮对话
    # ══════════════════════════════════════════════════════

    def _run_turn(self, segment) -> TurnResult:
        result = TurnResult()
        # 这里【不再】记一个 started：本方法在播报线程启动后就返回，
        # 用它算出来的「合计」天生不完整。计时一律逐项写 timings。

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
                # 模型返回空是异常情况，必须让调用方知道 ——
                # 只打一条 warning 然后静默回待机，用户会觉得"她没理我"，
                # 而程序认为这一轮正常（与 M2 修过的静默成功是同一类问题）。
                log.warning("模型没有返回内容")
                result.failed = True
                result.error = "模型没有返回内容"
                self.stats.errors += 1
                self._emit(
                    EVT_ERROR,
                    message=result.error,
                    hint="可能是模型额度耗尽或请求被拒绝，可稍后重试",
                )
                self._set_state(AssistantState.IDLE)
                return result

            # ---- 启动播报（非阻塞）----
            # 这里**绝不能等待播报结束**：一旦阻塞，主线程就再也回不到
            # run() 的帧循环，SPEAKING 分支成了死代码，「说停就停」
            # 便永远不可能生效（这正是本次审查发现的 X1）。
            # 播报结束由 _tick_speaking() 检测并收尾。
            self._start_speaking(reply.sentences, result)

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

        # 【刻意不存 timings["合计"]】
        # _run_turn 在播报线程启动后就返回了，此刻「播报」这一项还没写完；
        # 存下来的合计既漏掉播报，又会在 timing_summary() 求和时被再加一遍
        # —— 与 M2 审查 B2 是同一类矛盾。合计一律由 timing_summary() 现算。
        return result

    def _start_speaking(self, sentences: list[str], result: TurnResult) -> None:
        """启动播报线程并**立即返回**。

        主线程随后回到 run() 的帧循环，SPEAKING 分支开始工作：
        一边检测「停」，一边等播报线程结束。
        """
        if not sentences:
            # 没有可播报的内容 = 这一轮什么都没说，必须让调用方知道，
            # 不能当成成功（M2 的 pipeline 已有这条判定，M3 一度漏掉）。
            raise ApiError(
                "没有可播报的内容",
                hint="模型回复经清洗后为空，可能内容全是表情或符号",
            )

        box: dict[str, Any] = {}
        self._speak_box = box
        self._speak_result = result
        self._speak_started = time.monotonic()

        def work() -> None:
            speaker = _SentenceSpeaker(self.tts, self.speaker, self._event_sink)
            try:
                # 传快照：run() 现在需要 len() 才能发出 tts_done 的 total
                box["result"] = speaker.run(list(sentences))
            except BaseException as exc:  # noqa: BLE001 — 交给主线程统一处理
                box["error"] = exc

        self._speak_thread = threading.Thread(target=work, daemon=True, name="ayuan-speak")
        self._set_state(AssistantState.SPEAKING)
        self._speak_thread.start()

    def _tick_speaking(self, frame: bytes) -> None:
        """播报期间每帧调用：检测「停」＋ 检查播报是否结束。"""
        thread = self._speak_thread
        if thread is None or not thread.is_alive():
            self._finish_speaking()
            return

        if self.stop_detector.feed(frame):
            self.stop_detector.reset()
            log.info("收到「停」指令，中断播报")
            self.stats.interrupted += 1
            self._emit(EVT_INTERRUPTED)
            self.speaker.stop()

    def _finish_speaking(self) -> None:
        """播报结束（自然结束或被打断）后的收尾。"""
        thread = self._speak_thread
        box = self._speak_box
        result = self._speak_result
        self._speak_thread = None
        self._speak_box = {}
        self._speak_result = None

        if thread is not None:
            thread.join(timeout=2.0)

        error = box.get("error") if box else None
        audio, played, interrupted = (
            box.get("result", (0.0, 0, False)) if box else (0.0, 0, False)
        )

        if result is not None:
            result.timings["播报"] = time.monotonic() - self._speak_started
            result.audio_seconds = audio
            result.played = played
            result.interrupted = interrupted

        self.stats.turns += 1
        self.segmenter.reset()
        self._drain_frames()
        self._last_activity = time.monotonic()

        if error is not None:
            self.stats.errors += 1
            self._emit(
                EVT_ERROR,
                message=f"{type(error).__name__}: {error}",
                hint=getattr(error, "hint", ""),
            )
            if self.state not in (AssistantState.EXITING,):
                self._set_state(AssistantState.IDLE)
        else:
            # 回到聆听 —— 这是「连续对话」的关键：不必再喊唤醒词
            self._set_state(AssistantState.LISTENING)

    def _drain_frames(self) -> None:
        """丢弃积压的音频帧。

        播报期间输入线程仍在产帧，但它们多半是喇叭声或用户等待时的
        杂音，不该被当成下一轮的输入。
        """
        dropped = 0
        while True:
            try:
                self._frames.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            log.debug("丢弃积压音频 %d 帧", dropped)

    def close(self) -> None:
        self._running = False
        self.speaker.stop()
        self.microphone.close()
