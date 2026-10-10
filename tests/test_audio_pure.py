"""L2 设备层纯函数测试（无需硬件）。"""

from __future__ import annotations

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
