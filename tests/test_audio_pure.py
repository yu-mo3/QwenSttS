"""L2 设备层纯函数测试（无需硬件）。"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from app.audio.capture import Microphone
from app.audio.player import Speaker, parse_audio
from app.config import AudioSettings


class TestRms:
    """均方根振幅。审查发现的崩溃点：奇数字节。"""

    @pytest.mark.parametrize(
        "data",
        [
            b"",                      # 空
            b"\x01",                  # 1 字节（奇数）
            b"\x01\x02\x03",          # 3 字节（奇数）
            b"\x00",                  # 1 字节全零
            b"\x00\x00",              # 正常一帧
            b"\xff" * 7,              # 7 字节
        ],
    )
    def test_never_raises(self, data: bytes) -> None:
        """任意字节长度都不能抛异常。

        np.frombuffer 遇到奇数字节会抛原生 ValueError，它不属于
        AyuanError 家族 —— 上层按 AudioDeviceError 写的容错完全兜不住。
        """
        assert Microphone.rms(data) >= 0.0

    def test_empty_is_zero(self) -> None:
        assert Microphone.rms(b"") == 0.0

    def test_silence_is_zero(self) -> None:
        assert Microphone.rms(b"\x00\x00" * 100) == 0.0

    def test_full_scale_is_one(self) -> None:
        """满量程方波的 RMS 应为 1.0。"""
        pcm = np.full(100, 32767, dtype=np.int16).tobytes()
        assert Microphone.rms(pcm) == pytest.approx(1.0, abs=1e-3)

    def test_louder_signal_has_higher_rms(self) -> None:
        quiet = np.full(100, 100, dtype=np.int16).tobytes()
        loud = np.full(100, 10000, dtype=np.int16).tobytes()
        assert Microphone.rms(loud) > Microphone.rms(quiet)


class TestPcmToFloat:
    @pytest.mark.parametrize("data", [b"", b"\x01", b"\x01\x02\x03"])
    def test_never_raises(self, data: bytes) -> None:
        result = Microphone.pcm_to_float(data)
        assert isinstance(result, np.ndarray)

    def test_range(self) -> None:
        pcm = np.array([-32768, 0, 32767], dtype=np.int16).tobytes()
        values = Microphone.pcm_to_float(pcm)
        assert values.min() >= -1.0
        assert values.max() <= 1.0


class TestParseAudio:
    """音频格式识别。"""

    def test_raw_pcm_passthrough(self) -> None:
        pcm, rate, channels = parse_audio(b"\x01\x02", 16000)
        assert (pcm, rate, channels) == (b"\x01\x02", 16000, 1)

    def test_empty(self) -> None:
        pcm, rate, _ = parse_audio(b"", 16000)
        assert pcm == b""
        assert rate == 16000


class TestSpeakerBeep:
    """提示音。审查发现的崩溃点：duration 极小时的 fade 广播。"""

    def test_too_short_beep(self) -> None:
        """duration 小到数组只有 1 个点时，淡入淡出数组无法广播。

        实测过 ValueError: could not broadcast input array from
        shape (80,) into shape (1,)。修复方式是给 fade 加一个上界。
        """
        # 用 __new__ 绕过 __init__（避免解析真实音频设备），只补上波形生成
        # 需要的 settings，然后把 play 换成记录器 —— 这样测试不碰硬件
        speaker = Speaker.__new__(Speaker)
        speaker.settings = AudioSettings()
        audio: list[bytes] = []
        speaker.play = lambda data, **kw: audio.append(data) or True  # type: ignore[method-assign]

        for duration in (0.0001, 0.001, 0.01, 0.12):
            audio.clear()
            speaker.beep(duration=duration)
            # 极短时长允许不发声（返回空），但绝不能抛异常
            assert len(audio) <= 1

def _devicefree_speaker(blocks: int = 4, stop_at: int | None = None):
    """造一个不碰硬件的 Speaker。

    用 __new__ 绕过 __init__（不解析真实音频设备），补上 play() 需要的
    运行时字段，再替换 _play_loop 成分块假播放。返回 (扬声器, 已播块号)。

    stop_at 指定在第几块之后调用 stop()，用来模拟「播放途中用户喊停」。
    """
    speaker = Speaker.__new__(Speaker)
    speaker.settings = AudioSettings()
    speaker._stop_event = threading.Event()
    speaker._playing = threading.Event()
    speaker._lock = threading.Lock()
    played: list[int] = []

    def fake_play_loop(pcm, rate, channels) -> None:
        for index in range(blocks):
            if speaker._stop_event.is_set():
                return
            played.append(index)
            if stop_at is not None and index == stop_at:
                speaker.stop()

    speaker._play_loop = fake_play_loop  # type: ignore[method-assign]
    return speaker, played


class TestSpeakerStopLatch:
    """KI-007②：句间窗口发出的「停」不能被静默丢掉。

    修复前 stop() 只在 _playing 已置位时才置位停止事件，而 play() 入口
    又会 clear() 它 —— 于是「上一句刚播完、下一句还没开始」那一小段窗口里
    发出的停止请求会被丢掉：用户喊了「停」，她 200ms 后接着念下一句。
    """

    def test_stop_before_play_skips_the_utterance(self) -> None:
        speaker, played = _devicefree_speaker()
        speaker.stop()          # 句间窗口发出的停止请求
        assert speaker.play(b"\x01\x02" * 100) is False
        assert played == [], "一个字都不该播"
        assert speaker.is_playing is False, "跳过时不该留下播放中的状态"

    def test_latch_is_consumed_by_the_next_play(self) -> None:
        """闭锁只作用于当前这一段，不能把后续每一次播放都吃掉。"""
        speaker, played = _devicefree_speaker()
        speaker.stop()
        assert speaker.play(b"\x01\x02" * 100) is False
        assert speaker.play(b"\x01\x02" * 100) is True
        assert played, "第二段应当正常播放"

    def test_clear_stop_reenables_playback(self) -> None:
        speaker, played = _devicefree_speaker()
        speaker.stop()
        speaker.clear_stop()    # 新一段播报 / 新会话开始
        assert speaker.play(b"\x01\x02" * 100) is True
        assert played

    def test_stop_during_playback_still_interrupts(self) -> None:
        speaker, played = _devicefree_speaker(blocks=4, stop_at=1)
        assert speaker.play(b"\x01\x02" * 100) is False
        assert played == [0, 1], f"应当在第 2 块后停下，实际播了 {played}"

    def test_full_playback_returns_true(self) -> None:
        speaker, played = _devicefree_speaker(blocks=3)
        assert speaker.play(b"\x01\x02" * 100) is True
        assert played == [0, 1, 2]

    def test_empty_audio_is_a_noop_success(self) -> None:
        speaker, _ = _devicefree_speaker()
        assert speaker.play(b"") is True

    def test_stop_latches_when_idle(self) -> None:
        """空闲时也要置位 —— 这正是修复点。"""
        speaker, _ = _devicefree_speaker()
        assert speaker._stop_event.is_set() is False
        speaker.stop()
        assert speaker._stop_event.is_set() is True
