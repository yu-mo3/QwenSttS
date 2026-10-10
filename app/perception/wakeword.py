"""离线关键词唤醒：听到「阿源」就把助手叫醒。

## 为什么用 KWS 而不是「ASR + 拼音匹配」

原教程用的是 Vosk 做完整识别、再把结果转拼音去比对。本项目改用
sherpa-onnx 的关键词唤醒，原因很实际：

「阿源」只有两个音节。拼音比对方案下，**原来、员工、原因、公园、愿意、
美元、学员**……全都包含 yuan 音，任何一个都会把助手叫醒。
而 KWS 走的是声学模型的音素级匹配，对短词可靠得多，延迟也更低
（增量解码，不需要等整句识别完）。

## 关键词的 token 写法（踩坑记录）

关键词文件里写的不是汉字，而是**声母 + 韵母**的 token 序列：

    n ǚ ér @女儿          -> 女(n+ǚ) + 儿(ér，零声母)
    f ǎ g uó @法国        -> 法(f+ǎ) + 国(g+uó)

    ā y uán @阿源         -> 阿(ā，零声母) + 源(y+uán)

**「源」的声母 y 必须写出来。** 写成 ā uán 完全不触发 —— 这一点
在官方文档里没有明说，是拿真人录音一点点试出来的
（穷举了 10 种拆分方式，只有 ā y uán 能唤醒）。

阈值实测（同一段真人「阿源」录音）：
    0.01 ~ 0.2  可触发
    0.3 以上    不触发
默认取 0.2 —— 在能触发的前提下取最高值，以压低误触发率。

## 一个必须知道的限制

KWS 模型**只认真人语音**。实测：同一句话，真人录音能唤醒，
TTS 合成的完全相同内容唤不醒（已用云端 ASR 交叉验证过 TTS 音频正常）。
这意味着唤醒词无法用 TTS 闭环自测，改动关键词后必须真人复测。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.config import MODELS_DIR, Config
from app.core.errors import AyuanError
from app.core.logger import get_logger

log = get_logger(__name__)

# 模型目录名（与 sherpa-onnx 官方发布包保持一致）
DEFAULT_MODEL_DIR = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
DEFAULT_KEYWORDS_FILE = "keywords_ayuan.txt"

# 阈值：实测可触发区间 0.01 ~ 0.2，取上界以压低误触发
DEFAULT_THRESHOLD = 0.2


class ModelNotFoundError(AyuanError):
    """唤醒模型缺失。"""


@dataclass(frozen=True)
class WakeWordConfig:
    """唤醒器配置。"""

    model_dir: Path
    keywords_file: Path
    threshold: float = DEFAULT_THRESHOLD
    num_threads: int = 2

    @property
    def tokens(self) -> Path:
        return self.model_dir / "tokens.txt"

    def encoder(self) -> Path:
        return self.model_dir / "encoder-epoch-13-avg-2-chunk-16-left-64.onnx"

    def decoder(self) -> Path:
        return self.model_dir / "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"

    def joiner(self) -> Path:
        return self.model_dir / "joiner-epoch-13-avg-2-chunk-16-left-64.onnx"

    def missing(self) -> list[Path]:
        """列出缺失的文件，便于一次性告诉用户缺什么。"""
        return [p for p in (self.tokens, self.encoder(), self.decoder(),
                            self.joiner(), self.keywords_file) if not p.exists()]


def default_config(config: Config | None = None, threshold: float | None = None) -> WakeWordConfig:
    """构造默认配置。

    threshold 传 None 时用 DEFAULT_THRESHOLD —— 这样调用方可以直接把
    "用户没指定"透传进来，不必自己兜底默认值。
    （config 参数目前未使用，保留是为了将来支持把模型路径写进 .env。）
    """
    return WakeWordConfig(
        model_dir=MODELS_DIR / DEFAULT_MODEL_DIR,
        keywords_file=MODELS_DIR / DEFAULT_KEYWORDS_FILE,
        threshold=DEFAULT_THRESHOLD if threshold is None else threshold,
    )


class WakeWordDetector:
    """逐帧喂音频，听到唤醒词返回 True。

    使用方式（必须每帧只调一次 feed —— KWS 的流是有状态的）：

        detector = WakeWordDetector()
        for frame in microphone.frames():
            if detector.feed(frame):
                ...  # 醒了
                detector.reset()
    """

    def __init__(self, cfg: WakeWordConfig | None = None) -> None:
        self.cfg = cfg or default_config()

        missing = self.cfg.missing()
        if missing:
            raise ModelNotFoundError(
                "唤醒模型不完整",
                hint=(
                    "缺少：" + "、".join(p.name for p in missing) + "。"
                    "请运行 tools/download_models.py 下载，"
                    "或手动放到 models/ 下。"
                ),
            )

        import sherpa_onnx  # 延迟导入：没装依赖时也能 import 本模块

        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(self.cfg.tokens),
            encoder=str(self.cfg.encoder()),
            decoder=str(self.cfg.decoder()),
            joiner=str(self.cfg.joiner()),
            num_threads=self.cfg.num_threads,
            keywords_file=str(self.cfg.keywords_file),
            keywords_score=1.0,
            keywords_threshold=self.cfg.threshold,
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()
        self._sample_rate = 16000
        log.info(
            "唤醒器就绪：阈值 %.2f，关键词来自 %s",
            self.cfg.threshold, self.cfg.keywords_file.name,
        )

    def feed(self, frame: bytes) -> bool:
        """喂入一帧 16bit PCM（16kHz），检测到唤醒词返回 True。

        帧长建议 20ms（320 采样点）。KWS 支持任意长度，但固定帧长
        能让延迟更可预测。
        """
        if not frame:
            return False
        samples = np.frombuffer(
            frame[: len(frame) - (len(frame) % 2)], dtype=np.int16
        ).astype(np.float32) / 32768.0
        if samples.size == 0:
            return False

        self._stream.accept_waveform(self._sample_rate, samples)
        hit = False
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                log.info("检测到唤醒词：%s", result)
                hit = True
        return hit

    def reset(self) -> None:
        """清空解码状态。唤醒后必须调用，否则可能立刻重复触发。"""
        self._stream = self._spotter.create_stream()

    @property
    def keywords(self) -> str:
        try:
            return self.cfg.keywords_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
