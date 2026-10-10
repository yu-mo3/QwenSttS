"""阿源 · 全局配置。

优先级：真实环境变量 > .env 文件 > 本文件默认值。
（load_dotenv 默认 override=False，因此进程里已存在的变量优先级更高。）

设计意图：所有「会变的东西」（密钥、模型名、设备名、音频参数）都收敛到
这里，业务代码只读不写。换模型、换音色、换麦克风都只动这一个文件或 .env。

**值对象保持纯净**：AudioSettings / ModelSettings / Secrets 的字段全是显式值，
不读环境变量 —— 要按环境构造请用各自的 from_env()。这样单测里写
`AudioSettings()` 拿到的是确定的默认值，而不是开发机 .env 的产物，
测试才是可复现的。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from app.core.logger import get_logger

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"

# .env 不存在时静默跳过——跑自检脚本不该因为还没配密钥就崩溃
load_dotenv(ENV_FILE)


# ══════════════════════════════════════════════════════════
#  环境变量读取
# ══════════════════════════════════════════════════════════

def _read_str(env: Mapping[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


def _read_float(env: Mapping[str, str], key: str, default: float) -> float:
    """读浮点配置。非法值回落并**留痕**。

    静默回落是「非程序员用户」最难排查的一类问题：把
    AYUAN_RECORD_SECONDS 写成 "3s"，行为与预期不符却查不出原因。
    """
    raw = _read_str(env, key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("配置项 %s 的值 %r 不是合法数字，已回落到默认值 %r", key, raw, default)
        return default


# ══════════════════════════════════════════════════════════
#  密钥脱敏
# ══════════════════════════════════════════════════════════

def mask_secret(secret: str) -> str:
    """密钥脱敏，用于打印。

    只保留厂商前缀，**首尾明文一律不露**。
    本机密钥 117 位时露 8+4 位影响不大，但工程上不能赌密钥长度 ——
    DashScope 常见 35 位密钥，露 12 位就是 34%。
    而自检输出恰恰是用户求助时最常截图/粘贴的内容。
    """
    if not secret:
        return "未配置"
    prefix = secret[:3] if secret.startswith("sk-") else ""
    return f"{prefix}***（共 {len(secret)} 字符）"


# ══════════════════════════════════════════════════════════
#  音频
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class AudioSettings:
    """音频链路参数（纯值对象，不读环境）。

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
    input_device: str = ""
    output_device: str = ""
    record_seconds: float = 3.0

    @property
    def frame_samples(self) -> int:
        """单帧采样点数。"""
        return int(self.sample_rate * self.frame_ms / 1000)

    @property
    def frame_bytes(self) -> int:
        """单帧字节数。"""
        return self.frame_samples * self.sample_width * self.channels

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AudioSettings":
        env = os.environ if env is None else env
        return cls(
            input_device=_read_str(env, "AYUAN_INPUT_DEVICE"),
            output_device=_read_str(env, "AYUAN_OUTPUT_DEVICE"),
            record_seconds=_read_float(env, "AYUAN_RECORD_SECONDS", 3.0),
        )


# ══════════════════════════════════════════════════════════
#  模型
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ModelSettings:
    """阿里云百炼（DashScope）模型清单（纯值对象，不读环境）。

    两类端点并存，别搞混：
      · compatible_url —— OpenAI 兼容协议，供 LLM / VLM 使用（openai SDK）
      · native_url     —— 百炼原生协议，供 ASR / TTS 使用（requests）
                         实测 OpenAI 兼容端点不提供 /audio/speech 与
                         /audio/transcriptions。

    ⚠️ enable_thinking 必须保持 False：同一条请求，开启思考后首字延迟
       从 0.46s 涨到 4.46s，语音助手会卡成 PPT。
    """

    compatible_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    native_url: str = "https://dashscope.aliyuncs.com/api/v1"

    llm: str = "qwen3.8-27b"
    asr: str = "qwen3-asr-flash-2026-02-10"
    tts: str = "qwen3-tts-flash"
    vlm: str = "qwen3-vl-plus"
    tts_voice: str = "Cherry"

    enable_thinking: bool = False
    # 语音助手不该等 60 秒。20s 已足够覆盖网络抖动，
    # 再长只会让「重试 3 次」的最坏情况变成 3 分钟静默挂起。
    timeout: int = 20

    # 助手名称：唤醒词与提示词共用同一个来源
    name: str = "阿源"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ModelSettings":
        env = os.environ if env is None else env
        return cls(
            llm=_read_str(env, "AYUAN_LLM", cls.llm),
            asr=_read_str(env, "AYUAN_ASR_MODEL", cls.asr),
            tts=_read_str(env, "AYUAN_TTS_MODEL", cls.tts),
            vlm=_read_str(env, "AYUAN_VLM_MODEL", cls.vlm),
            tts_voice=_read_str(env, "AYUAN_TTS_VOICE", cls.tts_voice),
        )


# ══════════════════════════════════════════════════════════
#  感知与交互
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class PerceptionSettings:
    """唤醒 / 打断 / 静默超时（纯值对象，不读环境）。

    这三个值原先只存在于命令行参数里（tools/m3_assistant.py --threshold），
    界面化之后必须有个正式归属 —— 否则 M4 的设置面板要么没法保存，
    要么就得自己另造一套配置来源，那是「两个事实来源」的老问题。

    stop_threshold 为 None 表示「与唤醒阈值相同」。这不是偷懒：
    实测两者能触发的区间本来就是同一个（0.01~0.2），
    分开设默认值只会让用户多调一个不知道该怎么调的旋钮。
    """

    wake_threshold: float = 0.2
    stop_threshold: float | None = None
    idle_timeout: float = 30.0

    @property
    def effective_stop_threshold(self) -> float:
        return (
            self.wake_threshold
            if self.stop_threshold is None
            else self.stop_threshold
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PerceptionSettings":
        env = os.environ if env is None else env
        raw_stop = _read_str(env, "AYUAN_STOP_THRESHOLD")
        return cls(
            wake_threshold=_read_float(env, "AYUAN_WAKE_THRESHOLD", cls.wake_threshold),
            stop_threshold=float(raw_stop) if _is_number(raw_stop) else None,
            idle_timeout=_read_float(env, "AYUAN_IDLE_TIMEOUT", cls.idle_timeout),
        )


def _is_number(raw: str) -> bool:
    if not raw:
        return False
    try:
        float(raw)
    except ValueError:
        return False
    return True


# ══════════════════════════════════════════════════════════
#  密钥
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Secrets:
    """密钥（纯值对象，不读环境）。全部来自 .env，代码里不留任何明文。"""

    dashscope_api_key: str = ""
    weather_api_key: str = ""

    @property
    def has_dashscope(self) -> bool:
        return bool(self.dashscope_api_key)

    @property
    def has_weather(self) -> bool:
        return bool(self.weather_api_key)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Secrets":
        env = os.environ if env is None else env
        return cls(
            dashscope_api_key=_read_str(env, "DASHSCOPE_API_KEY"),
            weather_api_key=_read_str(env, "WEATHER_API_KEY"),
        )


# ══════════════════════════════════════════════════════════
#  总入口
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    """配置总入口。"""

    audio: AudioSettings = field(default_factory=AudioSettings)
    model: ModelSettings = field(default_factory=ModelSettings)
    perception: PerceptionSettings = field(default_factory=PerceptionSettings)
    secrets: Secrets = field(default_factory=Secrets)
    log_level: str = "INFO"
    project_root: Path = PROJECT_ROOT
    data_dir: Path = DATA_DIR
    models_dir: Path = MODELS_DIR

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "Config":
        """从环境装配配置。

        传 env 可注入一份自定义环境（测试用），不传则读进程环境。
        """
        env = os.environ if env is None else env
        return cls(
            audio=AudioSettings.from_env(env),
            model=ModelSettings.from_env(env),
            perception=PerceptionSettings.from_env(env),
            secrets=Secrets.from_env(env),
            log_level=_read_str(env, "AYUAN_LOG_LEVEL", "INFO").upper(),
        )

    def ensure_dirs(self) -> None:
        """创建运行时需要的目录。"""
        for path in (self.data_dir, self.models_dir):
            path.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════
#  .env 回写
# ══════════════════════════════════════════════════════════

def sanitize_env_value(value: object) -> str:
    """把值规整成能安全放进 .env 单行的形式。

    **换行必须被吃掉**：一个带 \n 的值会把后面的内容变成新的配置行，
    等于让调用方（以及将来的任何输入源）能往用户的 .env 里注入任意键。
    """
    text = "" if value is None else str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def update_env_file(
    updates: Mapping[str, object],
    path: Path | None = None,
) -> Path:
    """把若干键写回 .env，**保留其它所有行与注释的原样**。

    为什么是行级改写而不是重新生成整个文件：.env 里可能有人手写的注释、
    临时开关、别的工具用的键。重新生成等于把它们全删了，而用户往往
    要到下一次调试才发现。

    为什么不另建一份 json 设置文件：配置只能有**一个**事实来源。
    界面把值写进 json、启动时读的却是 .env，两边不一致时
    谁也说不清「现在到底用的哪个」。

    写入走「先写临时文件再替换」：中途崩溃时不会留下一个被截断的
    .env —— 那会把用户的密钥一起弄丢。
    """
    target = ENV_FILE if path is None else Path(path)

    existing: list[str] = []
    if target.exists():
        existing = target.read_text(encoding="utf-8").splitlines()

    remaining = {key: sanitize_env_value(value) for key, value in updates.items()}

    lines: list[str] = []
    for line in existing:
        key = _env_key_of(line)
        if key is not None and key in remaining:
            lines.append(f"{key}={remaining.pop(key)}")
        else:
            lines.append(line)
    # 文件末尾原本没有换行时也不强行补空行，保持「改了几行就只动那几行」
    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    content = "\n".join(lines)
    if content:
        content += "\n"

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(target)
    log.info("已把 %d 项设置写回 %s", len(updates), target.name)
    return target


def _env_key_of(line: str) -> str | None:
    """从一行 .env 里取出键名；不是赋值行就返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key = stripped.split("=", 1)[0].strip()
    if key.startswith("export "):
        key = key[len("export ") :].strip()
    return key or None
