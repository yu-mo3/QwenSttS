
"""M3 感知层与状态机的单元测试。

设计原则：**分开测"逻辑"与"信号"**

  · 分段器的计数/缓冲/转换逻辑  -> 用可控的 VAD 替身（StubVad）测，
    确定性、不受信号特征影响
  · VAD 本身对噪声的判别        -> 用真实随机信号测

反面教材：一开始用「220Hz 正弦波」当语音样本去测分段器，6 个用例全挂 ——
VAD 根本不认为那是人声（真实语音的频谱结构复杂得多）。
拿合成信号碰运气不是单元测试该做的事。

真人录音相关的验证在 tools/wake_test.py，因为 KWS 只认真人语音。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.orchestrator.states import (
    ALLOWED_TRANSITIONS,
    AssistantState,
    InvalidTransition,
    can_transition,
)
from app.perception.segmenter import SpeechSegmenter
from app.perception.vad import VadDecision, VoiceActivityDetector

FRAME_BYTES = 640  # 20ms @16k 16bit


# ══════════════════════════════════════════════════════
#  状态机
# ══════════════════════════════════════════════════════

class TestStateMachine:
    def test_normal_flow(self):
        """待机 -> 聆听 -> 思考 -> 说话 -> 聆听（连续对话）"""
        assert can_transition(AssistantState.IDLE, AssistantState.LISTENING)
        assert can_transition(AssistantState.LISTENING, AssistantState.THINKING)
        assert can_transition(AssistantState.THINKING, AssistantState.SPEAKING)
        assert can_transition(AssistantState.SPEAKING, AssistantState.LISTENING)

    def test_illegal_transitions_rejected(self):
        """非法转换必须被拒绝 —— 它们通常意味着并发 bug，而不是碰巧发生。"""
        assert not can_transition(AssistantState.IDLE, AssistantState.SPEAKING)
        assert not can_transition(AssistantState.EXITING, AssistantState.IDLE)
        assert not can_transition(AssistantState.THINKING, AssistantState.LISTENING)
        assert not can_transition(AssistantState.SPEAKING, AssistantState.THINKING)

    def test_idle_to_thinking_is_legal_for_typed_input(self):
        """M4：打字输入不经过唤醒与聆听，所以待机 -> 思考成了合法转换。

        这条断言是**刻意改的**，不是把测试改成迁就代码。原断言
        （not can_transition(IDLE, THINKING)）在只有语音入口时成立，
        但它锁的是实现现状而不是规则。现在多了一条真实存在的入口，
        规则就该跟着更新 —— 而「待机 -> 说话」依旧非法：
        没有思考就没有话可说。
        """
        assert can_transition(AssistantState.IDLE, AssistantState.THINKING)

    def test_any_state_can_exit(self):
        for state in AssistantState:
            if state == AssistantState.EXITING:
                continue
            assert can_transition(state, AssistantState.EXITING), f"{state} 应能退出"

    def test_same_state_allowed(self):
        for state in AssistantState:
            assert can_transition(state, state)

    def test_exiting_is_terminal(self):
        assert ALLOWED_TRANSITIONS[AssistantState.EXITING] == set()

    def test_error_carries_both_states(self):
        exc = InvalidTransition(AssistantState.IDLE, AssistantState.SPEAKING)
        assert exc.src == AssistantState.IDLE
        assert exc.dst == AssistantState.SPEAKING
        assert "待机" in str(exc) and "说话" in str(exc)


# ══════════════════════════════════════════════════════
#  VAD · 构造与能量
# ══════════════════════════════════════════════════════

class TestVadConstruction:
    def test_rejects_bad_frame_length(self):
        """WebRTC VAD 只接受 10/20/30ms，构造时就该拒绝。"""
        with pytest.raises(ValueError, match="帧长"):
            VoiceActivityDetector(frame_ms=15)

    def test_rejects_bad_sample_rate(self):
        with pytest.raises(ValueError, match="采样率"):
            VoiceActivityDetector(sample_rate=22050)

    def test_accepts_allowed_frame_lengths(self):
        for ms in (10, 20, 30):
            VoiceActivityDetector(frame_ms=ms)


class TestVadEnergy:
    def test_silence_is_zero(self):
        assert VoiceActivityDetector.energy(b"\x00" * 640) == 0.0

    @pytest.mark.parametrize("data", [b"", b"\x01", b"\x01\x02\x03"])
    def test_never_raises_on_odd_bytes(self, data):
        """奇数字节不能抛异常（M1 踩过的坑）。"""
        assert VoiceActivityDetector.energy(data) >= 0.0

    def test_louder_has_higher_energy(self):
        quiet = np.full(320, 100, dtype=np.int16).tobytes()
        loud = np.full(320, 10000, dtype=np.int16).tobytes()
        assert VoiceActivityDetector.energy(loud) > VoiceActivityDetector.energy(quiet)


class TestVadEnergyGate:
    """能量条件是确定性的，可以放心断言。

    **不要**用合成噪声去断言 VAD 自身的判别力 —— 它依赖连续帧的历史，
    孤立单帧调用得不到有意义的结果（实测两种调用方式互相矛盾，
    详见 vad.py 的说明）。VAD 的真实表现需要在真实音频流里评估。
    """

    @staticmethod
    def _noise(sigma, frames=25, seed=7):
        rng = np.random.default_rng(seed)
        out = []
        for _ in range(frames):
            x = rng.normal(0, sigma, 320).astype(np.float32)
            out.append((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
        return out

    def test_silence_rejected(self):
        vad = VoiceActivityDetector()
        assert sum(1 for f in [b"\x00" * FRAME_BYTES] * 25 if vad.is_speech(f)) == 0

    @pytest.mark.parametrize("sigma", [0.0005, 0.002, 0.005, 0.01])
    def test_low_energy_noise_always_rejected(self, sigma):
        """能量低于阈值时，一律不算语音 —— 与 VAD 怎么判无关。

        这是双条件里**确定性**的那一半，也是它真正的价值所在。
        """
        vad = VoiceActivityDetector()
        for frame in self._noise(sigma):
            decision = vad.decide(frame)
            assert decision.energy < vad.energy_threshold, "前提：能量应低于阈值"
            assert not decision.is_speech, "能量不足就不该判为语音"
            assert decision.reason.startswith("频谱像人声") or decision.reason == "静音"

    def test_high_energy_noise_not_gated_by_energy(self):
        """能量超过阈值时，能量条件不再拦截 —— 是否算语音交给 VAD。"""
        vad = VoiceActivityDetector()
        frame = self._noise(0.1, frames=1)[0]
        decision = vad.decide(frame)
        assert decision.energy_ok, "大能量应当通过能量门槛"
        # 不断言 is_speech：那取决于 VAD，而单帧调用不可靠

    def test_energy_threshold_configurable(self):
        """阈值可调：调高后同一段音频会被挡下。"""
        frames = self._noise(0.005)   # 能量约 130
        low = VoiceActivityDetector(energy_threshold=100)
        high = VoiceActivityDetector(energy_threshold=5000)
        assert any(low.decide(f).energy_ok for f in frames)
        assert not any(high.decide(f).energy_ok for f in frames)


# ══════════════════════════════════════════════════════
#  VAD · 内部状态（记录认知，不是断言行为必须如此）
# ══════════════════════════════════════════════════════

class TestVadInternalState:
    def test_repeated_call_returns_booleans(self):
        """webrtcvad 的流携带内部状态，同一帧连调可能得到不同结果。

        这条测试固化两个认知：
          1. 它不会崩
          2. 返回值始终是布尔
        如果哪天行为变了，说明依赖升级了，需要重新评估使用方式。
        """
        rng = np.random.default_rng(7)
        frame = (np.clip(rng.normal(0, 0.05, 320), -1, 1) * 32767).astype(np.int16).tobytes()
        vad = VoiceActivityDetector()
        results = [vad.is_speech(frame) for _ in range(6)]
        assert all(isinstance(r, bool) for r in results)


# ══════════════════════════════════════════════════════
#  分段器 · 用可控替身测逻辑
# ══════════════════════════════════════════════════════

class StubVad:
    """可控的 VAD 替身。

    输入是一串布尔（True=人声），每 feed 一帧就吐出下一个。
    这样分段器的计数/缓冲/状态转换逻辑可以被确定性地测试，
    不受真实信号特征的影响。
    """

    def __init__(self, pattern: list[bool]) -> None:
        self.pattern = pattern
        self.index = 0
        self.sample_rate = 16000
        self.frame_ms = 20

    def decide(self, frame: bytes) -> VadDecision:
        value = self.pattern[min(self.index, len(self.pattern) - 1)]
        self.index += 1
        return VadDecision(value, value, 1000.0 if value else 0.0, value)

    def is_speech(self, frame: bytes) -> bool:
        return self.decide(frame).is_speech


def feed_all(seg: SpeechSegmenter, pattern: list[bool]):
    """按给定的人声/静音序列喂帧，返回产出的语音段。"""
    produced = []
    for _ in pattern:
        s = seg.feed(b"\x00" * FRAME_BYTES)
        if s:
            produced.append(s)
    tail = seg.flush()
    if tail:
        produced.append(tail)
    return produced


class TestSegmenterLogic:
    def test_pure_silence_produces_nothing(self):
        seg = SpeechSegmenter(StubVad([False] * 60))
        assert feed_all(seg, [False] * 60) == []

    def test_continuous_speech_produces_one_segment(self):
        seg = SpeechSegmenter(StubVad([True] * 60))
        assert len(feed_all(seg, [True] * 60)) == 1

    def test_speech_window_delays_start(self):
        """要连续 10 帧人声才确认开始 —— 单帧不该触发。"""
        seg = SpeechSegmenter(StubVad([True] * 9 + [False] * 40))
        assert not seg.is_speaking
        for _ in range(9):
            seg.feed(b"\x00" * FRAME_BYTES)
        assert not seg.is_speaking, "9 帧人声不该确认开始"

    def test_brief_pause_does_not_split(self):
        """说话中的自然停顿（100ms = 5 帧）不该被当成说完了。"""
        pattern = [True] * 40 + [False] * 5 + [True] * 40 + [False] * 40
        seg = SpeechSegmenter(StubVad(pattern))
        assert len(feed_all(seg, pattern)) == 1

    def test_long_pause_splits(self):
        """停 800ms（40 帧）应切成两段。"""
        pattern = [True] * 40 + [False] * 40 + [True] * 40 + [False] * 40
        seg = SpeechSegmenter(StubVad(pattern))
        assert len(feed_all(seg, pattern)) == 2

    def test_too_short_discarded(self):
        """低于最短时长的声音（咳嗽、敲键盘）按噪声丢弃。"""
        pattern = [True] * 5 + [False] * 40
        seg = SpeechSegmenter(StubVad(pattern))
        assert feed_all(seg, pattern) == []

    def test_pre_buffer_kept(self):
        """前导缓冲要保住说话的开头 —— 否则首字被吃掉。"""
        # 先静音 20 帧填满前导缓冲，再说 30 帧
        pattern = [False] * 20 + [True] * 30 + [False] * 40
        seg = SpeechSegmenter(StubVad(pattern), pre_buffer=10)
        produced = feed_all(seg, pattern)
        assert len(produced) == 1
        # 收集的帧数应当多于纯人声帧数（因为接了前导缓冲）
        assert produced[0].frames > 30, f"应包含前导缓冲，实际 {produced[0].frames} 帧"

    def test_post_buffer_kept(self):
        """后导缓冲要保住尾音。"""
        pattern = [True] * 40 + [False] * 40
        seg = SpeechSegmenter(StubVad(pattern), post_buffer=10)
        produced = feed_all(seg, pattern)
        assert len(produced) == 1
        assert produced[0].frames > 40

    def test_rejects_inconsistent_window_config(self):
        """max 短于 min 时应当拒绝构造 —— 否则语音段永远无法产出。"""
        with pytest.raises(ValueError, match="max_speech_ms"):
            SpeechSegmenter(StubVad([True]), min_speech_ms=300, max_speech_ms=200)

    def test_max_duration_forces_finish(self):
        """超过上限强制收尾，防止无限录音。"""
        seg = SpeechSegmenter(StubVad([True] * 200), max_speech_ms=600)  # 30 帧上限（须 > min 300ms）
        produced = feed_all(seg, [True] * 200)
        assert produced, "达到上限应当强制产出"

    def test_flush_when_not_speaking(self):
        seg = SpeechSegmenter(StubVad([False]))
        seg.feed(b"\x00" * FRAME_BYTES)
        assert seg.flush() is None

    def test_flush_while_speaking(self):
        seg = SpeechSegmenter(StubVad([True] * 40))
        for _ in range(40):
            seg.feed(b"\x00" * FRAME_BYTES)
        assert seg.is_speaking
        assert seg.flush() is not None

    def test_reset_clears_state(self):
        seg = SpeechSegmenter(StubVad([True] * 40))
        for _ in range(40):
            seg.feed(b"\x00" * FRAME_BYTES)
        seg.reset()
        assert not seg.is_speaking
        assert seg.buffered_ms == 0

    def test_speech_window_configurable(self):
        """窗口可配：3 帧就该确认开始。"""
        seg = SpeechSegmenter(StubVad([True] * 40), speech_window=3)
        for _ in range(3):
            seg.feed(b"\x00" * FRAME_BYTES)
        assert seg.is_speaking

    def test_segment_duration_is_accurate(self):
        """时长的算法：帧数 x 20ms。"""
        seg = SpeechSegmenter(StubVad([True] * 60))
        produced = feed_all(seg, [True] * 60)
        assert len(produced) == 1
        expected = produced[0].frames * 20 / 1000
        assert abs(produced[0].duration - expected) < 1e-6


# ══════════════════════════════════════════════════════
#  分段器 · 帧序列完整性（X3 回归）
# ══════════════════════════════════════════════════════

def numbered_frame(index: int) -> bytes:
    """生成内容可识别的帧：640 字节全是这个序号（16bit 小端）。

    为什么需要它：原先的测试桩帧内容全是 b"\x00" * 640，
    于是「同一帧被拼进语音段两次」这种 bug **物理上不可能被发现** ——
    重复 100 遍也和一遍长得一样。带序号之后一眼就能看出来。
    """
    return index.to_bytes(2, "little") * 320


def frame_indices(pcm: bytes) -> list[int]:
    """把语音段的 PCM 还原成帧序号序列。"""
    return [
        int.from_bytes(pcm[i : i + 2], "little")
        for i in range(0, len(pcm) - 1, FRAME_BYTES)
    ]


class TestSegmenterNoDuplication:
    """X3 回归：语音段里不能出现重复帧。

    真实症状：送进 ASR 的开头被念两遍（"你你你好"）。
    根因曾经是——确认说话前，同一帧同时进了 collected 与前导缓冲，
    确认时又做 collected = pre_buffer + collected。
    """

    def _run(self, pattern, **kw):
        seg = SpeechSegmenter(StubVad(pattern), **kw)
        produced = []
        for i in range(len(pattern)):
            s = seg.feed(numbered_frame(i))
            if s:
                produced.append(s)
        tail = seg.flush()
        if tail:
            produced.append(tail)
        return produced

    def test_no_duplicate_frames_at_head(self):
        """开头不能重复。"""
        pattern = [False] * 20 + [True] * 40 + [False] * 40
        produced = self._run(pattern)
        assert len(produced) == 1
        idx = frame_indices(produced[0].pcm)
        assert len(idx) == len(set(idx)), (
            f"语音段内有重复帧：{sorted(i for i in set(idx) if idx.count(i) > 1)}"
        )

    def test_frame_sequence_is_monotonic(self):
        """帧序号必须单调递增 —— 既不能重复，也不能乱序。"""
        pattern = [False] * 20 + [True] * 40 + [False] * 40
        produced = self._run(pattern)
        idx = frame_indices(produced[0].pcm)
        assert idx == sorted(idx), f"帧序不是递增的：{idx}"

    def test_head_keeps_every_speech_frame(self):
        """说话的第一个字不能被丢掉 —— 这是前导缓冲存在的理由。"""
        pattern = [False] * 20 + [True] * 40 + [False] * 40
        produced = self._run(pattern, pre_buffer=10, speech_window=10)
        idx = frame_indices(produced[0].pcm)
        speech = [i for i in idx if 20 <= i <= 59]
        assert speech == list(range(20, 60)), (
            f"人声帧应完整且有序，实际缺 {sorted(set(range(20,60)) - set(speech))}"
        )
        # 组合是「40 帧人声 + 后导缓冲的 10 帧静音」。
        # 注意 pre_buffer == speech_window 时，确认瞬间缓冲里已经全是
        # 人声帧了（确认需要 10 帧人声），所以不会额外带出更早的静音。
        assert len(idx) == 40 + 10, f"应为 40 人声 + 10 后导静音，实际 {len(idx)}"

    def test_larger_pre_buffer_also_captures_leading_silence(self):
        """前导缓冲大于确认窗口时，才会额外保留说话前的静音。"""
        pattern = [False] * 20 + [True] * 40 + [False] * 40
        produced = self._run(pattern, pre_buffer=20, speech_window=10)
        idx = frame_indices(produced[0].pcm)
        # 缓冲 20 帧 > 窗口 10 帧，取到序号 10..29，因此含 10..19 的静音
        assert min(idx) == 10, f"应当带出更早的静音帧，实际起点 {min(idx)}"
        assert len(idx) == len(set(idx)), "仍不能有重复"

    def test_tail_contains_bounded_post_buffer(self):
        """尾部静音只保留后导缓冲那么多，不是整个静音窗口。"""
        pattern = [True] * 40 + [False] * 40
        produced = self._run(pattern, post_buffer=10, silence_window=30)
        idx = frame_indices(produced[0].pcm)
        tail = [i for i in idx if i >= 40]
        assert len(tail) == 10, f"尾部静音应只保留 10 帧，实际 {len(tail)}"
        assert tail == sorted(tail) and len(tail) == len(set(tail))

    def test_two_segments_both_clean(self):
        """切成两段时，每段自身都不能有重复。"""
        pattern = [True] * 40 + [False] * 40 + [True] * 40 + [False] * 40
        produced = self._run(pattern)
        assert len(produced) == 2
        for k, seg in enumerate(produced):
            idx = frame_indices(seg.pcm)
            assert len(idx) == len(set(idx)), f"第 {k+1} 段有重复帧"

    def test_full_sequence_partition(self):
        """两段合起来不应遗漏人声帧（可以重叠缓冲，但不能丢人声）。"""
        pattern = [True] * 40 + [False] * 40 + [True] * 40 + [False] * 40
        produced = self._run(pattern)
        all_idx = [i for seg in produced for i in frame_indices(seg.pcm)]
        speech = [i for i in all_idx if i < 40 or (80 <= i < 120)]
        assert len(speech) == len(set(speech)), "人声帧不应被重复收集"
        for i in range(40):
            assert i in speech, f"第 1 段丢了人声帧 {i}"
        for i in range(80, 120):
            assert i in speech, f"第 2 段丢了人声帧 {i}"

    def test_rejects_pre_buffer_smaller_than_window(self):
        """前导缓冲小于确认窗口会丢首字，应当拒绝构造。"""
        with pytest.raises(ValueError, match="pre_buffer"):
            SpeechSegmenter(StubVad([True]), pre_buffer=5, speech_window=10)


class TestSegmenterWithRealVad:
    """分段器 + 真实 VAD 的集成：验证两者能配合，不验证信号特征。"""

    def test_pure_silence(self):
        seg = SpeechSegmenter(VoiceActivityDetector())
        produced = []
        for _ in range(60):
            s = seg.feed(b"\x00" * FRAME_BYTES)
            if s:
                produced.append(s)
        assert produced == []

    def test_construction_from_real_vad(self):
        vad = VoiceActivityDetector()
        seg = SpeechSegmenter(vad)
        assert seg.frame_ms == vad.frame_ms
        assert seg.min_speech_frames == 15   # 300ms / 20ms
        assert seg.max_speech_frames == 750  # 15000ms / 20ms
