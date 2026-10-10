"""把连续的音频帧切成「一个完整的语音段」。

这是「免按钮交互」的核心：用户不需要按任何键，说完停顿一下，
系统自己知道他说完了。

状态机只有两个状态，但每个转换都需要**连续 N 帧**确认：

    静默中 ──连续 10 帧人声──> 说话中
    说话中 ──连续 30 帧静音──> 结束（产出语音段）

为什么要连续帧窗口，而不是一帧就切换：
    键盘声、咳嗽、椅子响都会产生个别「像人声」的帧。
    单帧触发会让系统频繁误启动；而说话途中的自然停顿（约 200ms）
    也不该被当成「说完了」。所以开始判定用短窗口（200ms），
    结束判定用长窗口（600ms）。

前后缓冲：
    前导缓冲 —— 保留触发前的若干帧。没有它，用户说的第一个字
                会被吃掉（因为要等 10 帧才确认"开始了"）。
    后导缓冲 —— 保留结束后的若干帧。没有它，尾音会被切掉。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logger import get_logger
from app.perception.vad import VoiceActivityDetector

log = get_logger(__name__)

# 连续多少帧人声才确认「开始说话」。10 帧 × 20ms = 200ms
DEFAULT_SPEECH_WINDOW = 10
# 连续多少帧静音才确认「说完了」。30 帧 × 20ms = 600ms
DEFAULT_SILENCE_WINDOW = 30
# 前导/后导缓冲帧数
DEFAULT_PRE_BUFFER = 10
DEFAULT_POST_BUFFER = 10
# 一段语音的最短时长（毫秒）。低于此值视为噪声，丢弃。
DEFAULT_MIN_SPEECH_MS = 300
# 一段语音的最长时长（毫秒）。超过则强制收尾，防止无限录音。
DEFAULT_MAX_SPEECH_MS = 15000


@dataclass
class SpeechSegment:
    """一段完整的语音。"""

    pcm: bytes
    sample_rate: int
    duration: float
    frames: int

    @property
    def is_empty(self) -> bool:
        return not self.pcm


@dataclass
class _State:
    """分段器的内部状态。抽成独立对象便于整体重置。"""

    speaking: bool = False
    speech_count: int = 0
    silence_count: int = 0
    collected: list[bytes] = field(default_factory=list)
    pre_buffer: list[bytes] = field(default_factory=list)
    post_buffer: list[bytes] = field(default_factory=list)
    total_frames: int = 0


class SpeechSegmenter:
    """逐帧喂入，凑成完整语音段时返回它。"""

    def __init__(
        self,
        vad: VoiceActivityDetector,
        *,
        speech_window: int = DEFAULT_SPEECH_WINDOW,
        silence_window: int = DEFAULT_SILENCE_WINDOW,
        pre_buffer: int = DEFAULT_PRE_BUFFER,
        post_buffer: int = DEFAULT_POST_BUFFER,
        min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
        max_speech_ms: int = DEFAULT_MAX_SPEECH_MS,
    ) -> None:
        self.vad = vad
        self.frame_ms = vad.frame_ms
        self.speech_window = speech_window
        self.silence_window = silence_window
        self.pre_buffer = pre_buffer
        self.post_buffer = post_buffer
        self.min_speech_frames = max(1, int(min_speech_ms / self.frame_ms))
        self.max_speech_frames = max(1, int(max_speech_ms / self.frame_ms))
        self._state = _State()

    # ---------- 喂数据 ----------

    def feed(self, frame: bytes) -> SpeechSegment | None:
        """喂入一帧。

        Returns:
            凑成完整语音段时返回它；否则返回 None。
        """
        decision = self.vad.decide(frame)
        state = self._state

        if decision.is_speech:
            state.speech_count += 1
            state.silence_count = 0
            state.post_buffer.clear()

            if not state.speaking and state.speech_count >= self.speech_window:
                state.speaking = True
                # 关键：把前导缓冲接在正式内容之前，否则首字被吃掉
                state.collected = list(state.pre_buffer) + state.collected
                state.pre_buffer.clear()
                log.debug("检测到说话开始（前导缓冲 %d 帧）", len(state.collected))

            if state.speaking:
                state.collected.append(frame)
                state.total_frames += 1
                if state.total_frames >= self.max_speech_frames:
                    log.info("语音段达到上限 %.1fs，强制收尾", self.max_speech_ms / 1000)
                    return self._finish()
            else:
                # 还没确认说话，但已经出现人声帧 —— 先攒着
                state.collected.append(frame)
                self._push_pre(frame)
        else:
            state.speech_count = 0

            if not state.speaking:
                self._push_pre(frame)
                state.collected.clear()
                state.total_frames = 0
            else:
                state.silence_count += 1
                state.collected.append(frame)
                self._push_post(frame)

                if state.silence_count >= self.silence_window:
                    return self._finish()

        return None

    def _push_pre(self, frame: bytes) -> None:
        buf = self._state.pre_buffer
        buf.append(frame)
        if len(buf) > self.pre_buffer:
            del buf[0 : len(buf) - self.pre_buffer]

    def _push_post(self, frame: bytes) -> None:
        buf = self._state.post_buffer
        buf.append(frame)
        if len(buf) > self.post_buffer:
            del buf[0 : len(buf) - self.post_buffer]

    # ---------- 收尾 ----------

    def _finish(self) -> SpeechSegment | None:
        state = self._state
        frames = state.collected
        # 关键：接上后导缓冲，否则尾音被切掉
        frames = frames + list(state.post_buffer)

        speech_frames = state.total_frames
        self._state = _State()

        if speech_frames < self.min_speech_frames:
            log.debug(
                "语音段过短（%d 帧 < %d 帧），按噪声丢弃",
                speech_frames, self.min_speech_frames,
            )
            return None

        pcm = b"".join(frames)
        duration = len(pcm) / (2 * self.vad.sample_rate)
        log.info(
            "语音段结束：%.2fs / %d 帧（有效语音 %d 帧）",
            duration, len(frames), speech_frames,
        )
        return SpeechSegment(
            pcm=pcm,
            sample_rate=self.vad.sample_rate,
            duration=duration,
            frames=len(frames),
        )

    def flush(self) -> SpeechSegment | None:
        """强制收尾（用户按了停止键，或程序要退出了）。"""
        if self._state.speaking:
            return self._finish()
        self._state = _State()
        return None

    def reset(self) -> None:
        self._state = _State()

    @property
    def is_speaking(self) -> bool:
        return self._state.speaking

    @property
    def buffered_ms(self) -> float:
        return self._state.total_frames * self.frame_ms
