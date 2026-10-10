"""WAV 编解码测试。

**这是最重要的一组回归测试。**

百炼 TTS 返回的是「流式 WAV」：RIFF 与 data chunk 的 size 字段被填成
0x7FFFFFxx（写入时大小未知的占位值），而不是真实字节数。Python 标准库
的 wave 模块会照单全收，导致 getnframes() 返回十亿量级 —— 时长显示成
44739 秒。
"""

from __future__ import annotations

import struct

import pytest

from app.audio.wav import (
    is_wav,
    parse_wav,
    pcm_duration,
    pcm_to_wav,
    wav_duration,
)

# 标准 WAV 里 data chunk size 字段的偏移：
# "RIFF"(4) + size(4) + "WAVE"(4) + "fmt "(4) + size(4) + 16字节fmt + "data"(4)
_DATA_SIZE_OFFSET = 40

# 百炼实际返回的假 size（观测值）
_BOGUS_SIZE = 0x7FFFFF9B


class TestRoundTrip:
    def test_encode_then_decode(self) -> None:
        pcm = b"\x01\x02" * 16000  # 1 秒 @16kHz 16bit 单声道
        wav = pcm_to_wav(pcm, 16000)
        info = parse_wav(wav)
        assert info is not None
        assert info.sample_rate == 16000
        assert info.channels == 1
        assert info.sample_width == 2
        assert info.frames == 16000
        assert info.duration == pytest.approx(1.0)
        assert info.pcm == pcm


class TestStreamingWav:
    """流式 WAV（假 size）—— 真实踩过的坑。"""

    def test_bogus_data_size_is_ignored(self) -> None:
        """data chunk 声称 0x7FFFFF9B 字节，实际只有 2000。

        必须按**实际剩余字节**截断，而不是采信头部声明。
        """
        pcm = b"\x00\x01" * 1000
        raw = bytearray(pcm_to_wav(pcm, 16000))
        struct.pack_into("<I", raw, _DATA_SIZE_OFFSET, _BOGUS_SIZE)

        info = parse_wav(bytes(raw))
        assert info is not None, "假 size 不应导致解析失败"
        assert info.frames == 1000, f"应解析出 1000 帧，实际 {info.frames}"
        assert info.duration == pytest.approx(1000 / 16000)

    def test_bogus_size_does_not_inflate_duration(self) -> None:
        """回归：曾经算出 44739 秒（12 小时）。"""
        pcm = b"\x00\x01" * 8000  # 0.5 秒
        raw = bytearray(pcm_to_wav(pcm, 24000))
        struct.pack_into("<I", raw, _DATA_SIZE_OFFSET, _BOGUS_SIZE)

        duration = wav_duration(bytes(raw))
        assert duration < 1.0, f"0.5 秒的音频不该算出 {duration} 秒"
        assert duration == pytest.approx(8000 / 24000)

    def test_bogus_riff_size_also_tolerated(self) -> None:
        """RIFF size 同样是假的（0x7FFFFE7F），不能影响解析。"""
        pcm = b"\x00\x01" * 500
        raw = bytearray(pcm_to_wav(pcm, 16000))
        struct.pack_into("<I", raw, 4, 0x7FFFFE7F)

        info = parse_wav(bytes(raw))
        assert info is not None
        assert info.frames == 500


class TestMalformed:
    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"RIFF",
            b"not a wav at all",
            b"RIFF\x00\x00\x00\x00WAVE",           # 只有头，没有 chunk
        ],
    )
    def test_returns_none_not_raises(self, data: bytes) -> None:
        assert parse_wav(data) is None
        assert wav_duration(data) == 0.0

    def test_truncated_data_chunk(self) -> None:
        """data 声称 200 字节，实际只有 4 —— 取实际可用量。"""
        raw = bytearray(pcm_to_wav(b"\x00" * 100, 16000))
        struct.pack_into("<I", raw, _DATA_SIZE_OFFSET, 200)
        info = parse_wav(bytes(raw[:48]))  # 截断到只剩 4 字节数据
        assert info is not None
        assert info.frames == 2


class TestHelpers:
    def test_is_wav(self) -> None:
        assert is_wav(pcm_to_wav(b"\x00\x00", 16000))
        assert not is_wav(b"\x00\x00")
        assert not is_wav(b"")

    def test_pcm_duration(self) -> None:
        assert pcm_duration(b"\x00" * 32000, 16000) == pytest.approx(1.0)
        assert pcm_duration(b"", 16000) == 0.0
        assert pcm_duration(b"\x00" * 3, 16000) == pytest.approx(1 / 16000)  # 丢弃半个采样点
