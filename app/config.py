"""阿源 · 全局配置。

优先级：真实环境变量 > .env 文件 > 本文件默认值。

设计意图：所有「会变的东西」（密钥、模型名、设备名、音频参数）都收敛到
这里，业务代码只读不写。换模型、换音色、换麦克风都只动这一个文件或 .env。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"

# .env 不存在时静默跳过——跑自检脚本不该因为还没配密钥就崩溃
load_dotenv(ENV_FILE)


def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _int(key: str, default: int) -> int:
    raw = _str(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    raw = _str(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class AudioSettings:
    """音频链路参数。

    16kHz / 单声道 / 16bit 是三方的唯一交集，改动前请确认三者都还支持：
      · WebRTC VAD —— 只接受 8k/16k/32k/48k 采样率
      · sherpa-onnx 唤醒 —— 模型按 16k 训练
      · 云端 ASR —— 16k 单声道识别率最佳

    frame_ms 同理：WebRTC VAD 只接受 10 / 20 / 30 毫秒。
    """

    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # 字节数，即 16bit
    frame_ms: int = 20
    input_device: str = field(default_factory=lambda: _str("AYUAN_INPUT_DEVICE"))
    output_device: str = field(default_factory=lambda: _str("AYUAN_OUTPUT_DEVICE"))
    record_seconds: float = field(default_factory=lambda: _float("AYUAN_RECORD_SECONDS", 3.0))

    @property
    def frame_samples(self) -> int:
        """单帧采样点数。"""
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        """单帧字节数。"""
        return self.frame_samples * self.sample_width * self.channels


@dataclass(frozen=True)
class ModelSettings:
    """阿里云百炼（DashScope）模型清单。

    两类端点并存，别搞混：
      · compatible_url —— OpenAI 兼容协议，供 LLM / VLM 使用（openai SDK）
      · native_url     —— 百炼原生协议，供 ASR / TTS 使用（requests）
                         实测 OpenAI 兼容端点不提供 /audio/speech 与 /audio/transcriptions。

    ⚠️ enable_thinking 必须保持 False：同一条请求，开启思考后首字延迟
       从 0.46s 涨到 4.46s，语音助手会卡成 PPT。
    """

    compatible_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    native_url: str = "https://dashscope.aliyuncs.com/api/v1"

    llm: str = field(default_factory=lambda: _str("AYUAN_LLM", "qwen3.8-27b"))
    asr: str = field(default_factory=lambda: _str("AYUAN_ASR_MODEL", "qwen3-asr-flash-2026-02-10"))
    tts: str = field(default_factory=lambda: _str("AYUAN_TTS_MODEL", "qwen3-tts-flash"))
    vlm: str = field(default_factory=lambda: _str("AYUAN_VLM_MODEL", "qwen3-vl-plus"))
    tts_voice: str = field(default_factory=lambda: _str("AYUAN_TTS_VOICE", "Cherry"))

    enable_thinking: bool = False
    timeout: int = 60

    # 助手名称：唤醒词与提示词共用同一个来源
    name: str = "阿源"


@dataclass(frozen=True)
class Secrets:
    """密钥。全部来自 .env，代码里不留任何明文。"""

    dashscope_api_key: str = field(default_factory=lambda: _str("DASHSCOPE_API_KEY"))
    weather_api_key: str = field(default_factory=lambda: _str("WEATHER_API_KEY"))

    @property
    def has_dashscope(self) -> bool:
        return bool(self.dashscope_api_key)

    @property
    def has_weather(self) -> bool:
        return bool(self.weather_api_key)


@dataclass(frozen=True)
class Config:
    """配置总入口。"""

    audio: AudioSettings = field(default_factory=AudioSettings)
    model: ModelSettings = field(default_factory=ModelSettings)
    secrets: Secrets = field(default_factory=Secrets)
    log_level: str = field(default_factory=lambda: _str("AYUAN_LOG_LEVEL", "INFO").upper())
    project_root: Path = PROJECT_ROOT
    data_dir: Path = DATA_DIR
    models_dir: Path = MODELS_DIR

    @classmethod
    def load(cls) -> "Config":
        return cls()

    def ensure_dirs(self) -> None:
        """创建运行时需要的目录。"""
        for path in (self.data_dir, self.models_dir):
            path.mkdir(parents=True, exist_ok=True)
