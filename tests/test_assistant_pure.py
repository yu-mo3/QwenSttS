
"""编排层（VoiceAssistant）的测试。

**为什么这一层必须有测试**：M3 独立审查发现的三个阻塞级缺陷
（「停」不可达、输入线程静默死亡、静默超时从不触发）**全部**落在这里，
而当时 142 条测试没有一条 import 过 assistant —— 感知层测得再细也拦不住。

全部用假麦克风 / 假扬声器 / 假唤醒器 / 假分段器驱动，
不碰硬件、不联网、不需要密钥。
"""

from __future__ import annotations

import threading
import time

import pytest

from app.config import Config
from app.orchestrator.assistant import VoiceAssistant
from app.orchestrator.states import AssistantState
from app.perception.segmenter import SpeechSegment

FRAME = b"\x00" * 640
SEGMENT = SpeechSegment(pcm=b"\x00" * 640 * 40, sample_rate=16000, duration=0.8, frames=40)


# ══════════════════════════════════════════════════════
#  测试替身
# ══════════════════════════════════════════════════════

class FakeMic:
    """持续供帧；可配置为只产 N 帧后「自然结束」（模拟设备失效）。"""

    def __init__(self, max_frames: int | None = None, interval: float = 0.02) -> None:
        self.max_frames = max_frames
        self.interval = interval
        self.closed = False

    def device_name(self) -> str:
        return "FakeMic"

    def open(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def frames(self):
        count = 0
        while self.max_frames is None or count < self.max_frames:
            time.sleep(self.interval)
            count += 1
            yield FRAME
        # 正常返回 —— 这正是 capture.frames() 读到设备失效时的行为


class FakeSpeaker:
    """play() 阻塞一段时间；stop() 令其提前返回。"""

    def __init__(self, play_seconds: float = 1.0) -> None:
        self.play_seconds = play_seconds
        self.stopped = threading.Event()
        self.play_calls = 0
        self.stop_calls = 0
        self.completed = 0
        self.interrupted = 0

    def device_name(self) -> str:
        return "FakeSpeaker"

    def play(self, audio=None, sample_rate=None) -> bool:
        self.play_calls += 1
        self.stopped.clear()
        started = time.monotonic()
        while time.monotonic() - started < self.play_seconds:
            if self.stopped.is_set():
                self.interrupted += 1
                return False
            time.sleep(0.01)
        self.completed += 1
        return True

    def stop(self) -> None:
        self.stop_calls += 1
        self.stopped.set()


class FakeTts:
    class _Result:
        audio = b"x" * 2000
        duration = 1.0

        def __init__(self, empty: bool = False) -> None:
            self.is_empty = empty

    def __init__(self, empty: bool = False) -> None:
        self.empty = empty
        self.calls = 0

    def synthesize(self, text, **kw):
        self.calls += 1
        return self._Result(empty=self.empty)


class FakeWake:
    """第一帧就唤醒。"""

    def __init__(self) -> None:
        self.count = 0

    def feed(self, frame) -> bool:
        self.count += 1
        return self.count == 1

    def reset(self) -> None:
        pass


class FakeStop:
    """可配置为「一被喂帧就说停」或「从不说停」。"""

    def __init__(self, hits: bool = True) -> None:
        self.hits = hits
        self.count = 0

    def feed(self, frame) -> bool:
        self.count += 1
        return self.hits

    def reset(self) -> None:
        pass


class FakeSegmenter:
    """产出 N 次语音段后保持安静。

    产出次数可控，避免测试里出现「无限对话循环」——
    那会让静默计时被不断重置，静默超时的测试就测不出东西。
    """

    def __init__(self, times: int = 1) -> None:
        self.times = times
        self.produced = 0

    def feed(self, frame):
        if self.produced < self.times:
            self.produced += 1
            return SEGMENT
        return None

    def reset(self) -> None:
        pass  # 刻意不重置计数


class FakeAsr:
    def __init__(self, text: str = "你好") -> None:
        self.text = text

    def transcribe(self, pcm, rate, **kw):
        class T:
            pass

        result = T()
        result.text = self.text
        result.emotion = "neutral"
        result.is_empty = not self.text

        class S:
            elapsed = 0.0

        result.stats = S()
        return result


class FakeLlm:
    def __init__(self, sentences=None, text: str = "你好呀。") -> None:
        self.sentences = sentences if sentences is not None else ["你好呀。"]
        self.text = text

    def reply(self, user_text, memory):
        class R:
            pass

        result = R()
        result.text = self.text
        result.sentences = list(self.sentences)

        class S:
            first_byte = 0.05

        result.stats = S()
        return result


def make_assistant(
    *,
    mic: FakeMic | None = None,
    speaker: FakeSpeaker | None = None,
    tts: FakeTts | None = None,
    wake: FakeWake | None = None,
    stop: FakeStop | None = None,
    segmenter: FakeSegmenter | None = None,
    asr: FakeAsr | None = None,
    llm: FakeLlm | None = None,
    idle_timeout: float = 30.0,
) -> tuple[VoiceAssistant, dict]:
    """构造一个依赖全部被替换的助手。返回 (助手, 替身字典)。"""
    parts = {
        "mic": mic or FakeMic(),
        "speaker": speaker or FakeSpeaker(),
        "tts": tts or FakeTts(),
        "wake": wake or FakeWake(),
        "stop": stop or FakeStop(),
        "segmenter": segmenter or FakeSegmenter(),
        "asr": asr or FakeAsr(),
        "llm": llm or FakeLlm(),
    }
    assistant = VoiceAssistant(Config.load({}), idle_timeout=idle_timeout)
    assistant.microphone = parts["mic"]
    assistant.speaker = parts["speaker"]
    assistant.tts = parts["tts"]
    assistant.asr = parts["asr"]
    assistant.llm = parts["llm"]
    assistant._wake = parts["wake"]
    assistant._stop = parts["stop"]
    assistant._segmenter = parts["segmenter"]
    return assistant, parts


def start(assistant: VoiceAssistant) -> threading.Thread:
    thread = threading.Thread(target=assistant.run, daemon=True, name="test-run")
    thread.start()
    return thread


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def stop_after():
    """确保测试结束时不留下跑着的助手。"""
    running: list[VoiceAssistant] = []

    def _register(a: VoiceAssistant) -> VoiceAssistant:
        running.append(a)
        return a

    yield _register
    for a in running:
        try:
            a.stop()
            a.close()
        except Exception:
            pass
    time.sleep(0.2)


# ══════════════════════════════════════════════════════
#  X1 回归：播报期间能被「停」打断
# ══════════════════════════════════════════════════════

class TestInterrupt:
    def test_stop_detector_is_fed_during_speech(self, stop_after):
        """X1 回归。

        修复前的主线程在 _speak() 里被 join 阻塞，SPEAKING 分支是死代码，
        停用检测器在整个会话中被喂帧 **0 次**。
        """
        a = stop_after(make_assistant()[0])
        start(a)

        assert wait_for(lambda: a.state == AssistantState.SPEAKING), "应当进入播报状态"
        assert wait_for(lambda: a.speaker.stop_calls > 0), "「停」应当生效"

        assert a._stop.count > 0, f"停用检测器必须被喂帧，实际 {a._stop.count}"
        assert a.speaker.stop_calls > 0

    def test_interrupt_emits_event_and_counts(self, stop_after):
        a = stop_after(make_assistant()[0])
        events: list[str] = []
        a.on_event = lambda e, p: events.append(e)
        start(a)

        assert wait_for(lambda: "interrupted" in events)
        assert a.stats.interrupted >= 1

    def test_no_interrupt_when_stop_never_hits(self, stop_after):
        """没人说「停」时不该被打断。"""
        a = stop_after(make_assistant(stop=FakeStop(hits=False))[0])
        start(a)

        assert wait_for(lambda: a.speaker.completed > 0), "应正常播完"
        assert a.speaker.interrupted == 0
        assert a.stats.interrupted == 0

    def test_returns_to_listening_after_interrupt(self, stop_after):
        """被打断后要回到聆听，继续听用户说下一句。"""
        a = stop_after(make_assistant()[0])
        start(a)

        assert wait_for(lambda: a.speaker.stop_calls > 0)
        assert wait_for(lambda: a.state == AssistantState.LISTENING), (
            f"打断后应回到聆听，实际 {a.state}"
        )


# ══════════════════════════════════════════════════════
#  X2 回归：输入线程结束要让 run() 收敛
# ══════════════════════════════════════════════════════

class TestReaderTermination:
    def test_device_failure_stops_the_assistant(self, stop_after):
        """X2 回归。

        capture.frames() 在连续读空后会**正常 return**，不是抛异常。
        修复前读线程直接结束、无人处理，主循环空转 ——
        助手永久挂死：不报错、不退出、唤不醒。
        """
        a = stop_after(make_assistant(mic=FakeMic(max_frames=10))[0])
        events: list[tuple[str, dict]] = []
        a.on_event = lambda e, p: events.append((e, p))

        thread = start(a)
        thread.join(timeout=6.0)

        assert not thread.is_alive(), "run() 必须自己收敛，而不是永久空转"
        errors = [p for e, p in events if e == "error"]
        assert errors, "应当发出 error 事件说明输入已停止"
        assert "麦克风" in str(errors[0].get("message", ""))

    def test_no_hang_without_external_stop(self, stop_after):
        """不靠外部 stop() 也能自己退出。"""
        a = stop_after(make_assistant(mic=FakeMic(max_frames=5))[0])
        thread = start(a)
        thread.join(timeout=5.0)
        assert not thread.is_alive()


# ══════════════════════════════════════════════════════
#  X4 回归：静默超时回待机
# ══════════════════════════════════════════════════════

class TestIdleTimeout:
    def test_idle_returns_to_idle_state(self, stop_after):
        """X4 回归。

        修复前静默判定只在帧队列 Empty 时执行，而真实麦克风每 20ms 供一帧，
        get() 永远能在超时前返回 —— 6 秒内该函数被调用 0 次。
        """
        a = stop_after(make_assistant(idle_timeout=0.5)[0])
        events: list[str] = []
        a.on_event = lambda e, p: events.append(e)
        start(a)

        assert wait_for(lambda: a.state == AssistantState.LISTENING, timeout=3.0), "应先进入聆听"
        assert wait_for(lambda: "idle_timeout" in events, timeout=4.0), (
            "静默超时事件应当触发"
        )
        assert wait_for(lambda: a.state == AssistantState.IDLE, timeout=3.0), (
            f"应当回到待机，实际 {a.state}"
        )

    def test_activity_resets_the_timer(self, stop_after):
        """有对话活动时不该超时。"""
        a = stop_after(make_assistant(idle_timeout=5.0)[0])
        start(a)
        assert wait_for(lambda: a.stats.turns > 0, timeout=3.0)
        assert a.state != AssistantState.IDLE, "刚聊完不该立刻回待机"


# ══════════════════════════════════════════════════════
#  X8 回归：合成全空要判失败
# ══════════════════════════════════════════════════════

class TestEmptySynthesis:
    def test_all_empty_counts_as_error(self, stop_after):
        """X8 回归：一句都没合成出来时必须报错，不能当成成功。"""
        a = stop_after(make_assistant(tts=FakeTts(empty=True))[0])
        events: list[tuple[str, dict]] = []
        a.on_event = lambda e, p: events.append((e, p))
        start(a)

        assert wait_for(lambda: any(e == "error" for e, _ in events), timeout=4.0), (
            "全句合成为空应当发出 error 事件"
        )
        assert a.stats.errors >= 1

    def test_empty_sentence_list_is_error(self, stop_after):
        """回复清洗后没有句子时，同样要报错。"""
        a = stop_after(make_assistant(llm=FakeLlm(sentences=[], text=""))[0])
        events: list[str] = []
        a.on_event = lambda e, p: events.append(e)
        start(a)

        assert wait_for(lambda: "error" in events, timeout=4.0)


# ══════════════════════════════════════════════════════
#  连续对话与基本流转
# ══════════════════════════════════════════════════════

class TestContinuousConversation:
    def test_wake_then_speak_then_listen(self, stop_after):
        """唤醒 -> 思考 -> 说话 -> 聆听（不必再喊唤醒词）。"""
        a = stop_after(make_assistant(stop=FakeStop(hits=False))[0])
        seen: list[str] = []
        a.on_event = lambda e, p: seen.append(p.get("new", "")) if e == "state" else None
        start(a)

        assert wait_for(lambda: a.stats.turns >= 1, timeout=5.0), "应当完成至少一轮"
        assert "聆听" in seen, f"状态流转里应出现聆听，实际 {seen}"

    def test_bye_word_exits(self, stop_after):
        """说「再见」应当退出会话。"""
        a = stop_after(make_assistant(asr=FakeAsr(text="再见"))[0])
        thread = start(a)
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "说再见后应当自行退出"

    def test_empty_recognition_returns_to_idle(self, stop_after):
        """没识别出内容时回到待机，而不是傻等。"""
        a = stop_after(make_assistant(asr=FakeAsr(text=""))[0])
        start(a)
        assert wait_for(lambda: a.state == AssistantState.IDLE, timeout=4.0)


class TestThresholdParameter:
    """X6 回归：阈值必须是真实参数，不能是「看着生效」的空操作。"""

    def test_threshold_is_a_constructor_argument(self):
        a = VoiceAssistant(Config.load({}), wake_threshold=0.15)
        try:
            assert a.wake_threshold == 0.15, (
                f"构造时传入的阈值应当生效，实际 {a.wake_threshold}"
            )
        finally:
            a.close()

    def test_none_uses_default(self):
        from app.perception.wakeword import DEFAULT_THRESHOLD

        a = VoiceAssistant(Config.load({}))
        try:
            assert a.wake_threshold == DEFAULT_THRESHOLD
        finally:
            a.close()
