"""WAV 容器的编解码。

集中处理一个真实踩到的坑：**百炼 TTS 返回的是「流式 WAV」**。

它的 RIFF chunk 与 data chunk 的 size 字段被填成 `0x7FFFFFxx`
（表示「写入时大小未知」），而不是真实字节数。Python 标准库的
`wave` 模块会照单全收，于是：

    getnframes()  ->  1073741773   （十亿量级）
    getnframes()/getframerate()  ->  44739 秒   ← 时长显示成 12 小时
    readframes(getnframes())     ->  去申请 2GB 缓冲区  ← 潜在内存炸弹

**对策：只信任 fmt chunk，音频数据一律按「实际剩余字节」截断。**
本模块是项目中唯一允许解析 WAV 头的地方。

⚠️ **接口冻结提示**：L4 能力层（app/capabilities/base.py）依赖本模块的
pcm_to_wav / wav_duration。若要挪动这些纯编解码函数（例如上移到
app/core/），请同步修改能力层 —— 它们是跨层契约，不是设备层私有的。
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass

# RIFF 文件头长度：'RIFF' + size(4) + 'WAVE'
_RIFF_HEADER = 12

# 假 size 的判定阈值：超过 2^31 一律视为「流式占位值」
_SUSPICIOUS_SIZE = 0x7FFF_0000


@dataclass(frozen=True)
class WavInfo:
    """解析后的 WAV 参数与音频数据。"""

    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration: float
    pcm: bytes


def is_wav(data: bytes) -> bool:
    """判断是否为 WAV 容器（检查 RIFF/WAVE 魔数）。"""
    return len(data) > _RIFF_HEADER and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def pcm_to_wav(
    pcm: bytes,
    sample_rate: int,
    *,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """把裸 PCM 包成内存中的 WAV 容器。

    云端 ASR 只接受带容器头的音频；而我们的录音链路故意只产生裸 PCM
    （少一次拷贝），到出口这一步才加头 —— 全程不落盘。
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buffer.getvalue()


def _extract_data_chunk(data: bytes) -> bytes | None:
    """遍历 chunk，取出音频数据本体。

    刻意**不使用** data chunk 声称的 size：
    流式 WAV 会把它写成 0x7FFFFFxx，直接采信会读到文件末尾之外。
    """
    position = _RIFF_HEADER
    while position + 8 <= len(data):
        chunk_id = data[position : position + 4]
        chunk_size = int.from_bytes(data[position + 4 : position + 8], "little")
        body_start = position + 8
        available = len(data) - body_start

        if chunk_id == b"data":
            if chunk_size <= 0 or chunk_size > available:
                # size 不可信（流式占位或超出实际）→ 取到文件末尾
                return data[body_start:]
            return data[body_start : body_start + chunk_size]

        # 防御畸形的 chunk_size 把游标带飞
        if chunk_size <= 0 or chunk_size > available:
            return None
        position = body_start + chunk_size + (chunk_size & 1)  # chunk 按偶数字节对齐

    return None


def parse_wav(data: bytes) -> WavInfo | None:
    """解析 WAV。不是 WAV 或格式不支持时返回 None。"""
    if not is_wav(data):
        return None

    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
    except (wave.Error, EOFError):
        return None

    if sample_rate <= 0 or channels <= 0 or sample_width <= 0:
        return None

    pcm = _extract_data_chunk(data)
    if not pcm:
        return None

    frame_size = channels * sample_width
    frames = len(pcm) // frame_size
    pcm = pcm[: frames * frame_size]  # 丢掉不足一帧的尾巴

    return WavInfo(
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        frames=frames,
        duration=frames / sample_rate,
        pcm=pcm,
    )


def wav_duration(data: bytes) -> float:
    """WAV 时长（秒）。不是 WAV 或无法解析时返回 0。"""
    info = parse_wav(data)
    return info.duration if info else 0.0


def pcm_duration(pcm: bytes, sample_rate: int, *, channels: int = 1, sample_width: int = 2) -> float:
    """裸 PCM 时长（秒）。"""
    frame_size = channels * sample_width
    if frame_size <= 0 or sample_rate <= 0:
        return 0.0
    return (len(pcm) // frame_size) / sample_rate
