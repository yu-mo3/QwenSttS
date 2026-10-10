"""语音活动检测：判断「这一帧里有人在说话吗」。

用 WebRTC 的 VAD（Google 开源实现，轻量、无模型文件），但**不单独依赖它**。

## 为什么要叠加能量条件

能量条件是**确定性的**：帧能量低于阈值就一律不算语音，与 VAD 的
判断无关。这给噪声提供了一道可预期的下限保护 —— 静音与低电平
底噪不会因为 VAD 的偶然误判而启动录音。

## 一个必须说明的测试局限

VAD 的判别力**无法用合成噪声准确评估**。排查记录：

    调用方式          sigma=0.002   0.01    0.02
    ─────────────────────────────────────────────
    同一实例连调       4/40        0/40    4/40
    每帧新建实例      40/40        3/40   40/40

两种方式的结果互相矛盾，而且都不符合直觉（0.002 判满、0.01 几乎不判）。
根因是 **WebRTC VAD 依赖连续帧的历史**，孤立地单帧调用得不到有意义的结果。

因此：**不要用单帧噪声去测 VAD 的判别力**。它只应在真实音频流中使用，
其真实表现需要在真实噪声环境下实测（见 KNOWN_ISSUES）。

帧长限制：WebRTC VAD 只接受 10 / 20 / 30 毫秒，本项目统一用 20ms。

一个隐藏特性（实测踩到）：webrtcvad 的 Process() 在**连续调用之间携带
内部状态**（上一帧的特征缓存）。对同一帧连续调用两次会得到不同结果：

    同一个实例：  第一次=True   第二次=False   <- 不一致
    两个独立实例：实例A=True    实例B=True     <- 一致

正常流式使用不受影响（每帧只调一次）。但两条纪律必须遵守：

1. 同一帧**不要**重复调用 is_speech() 做对比 —— 会得到失真结论
2. **不要**让两处逻辑（例如唤醒检测与 VAD 断句）共用同一个实例，
   它们会互相污染状态；各建各的

排查这个问题花了不少时间：白噪声测试里出现「单帧判定全是人声、
分段器却一帧都没收到」的诡异现象，根因就是这个。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import webrtcvad

from app.core.logger import get_logger

log = get_logger(__name__)

# WebRTC VAD 只接受这三档帧长（毫秒）
ALLOWED_FRAME_MS = (10, 20, 30)

# 帧能量的默认下限。
# 16bit PCM 的满量程是 32767，正常说话的帧均值约 300~3000，
# 静音帧通常低于 100。300 是实测下来的稳妥分界。
DEFAULT_ENERGY_THRESHOLD = 300.0


@dataclass(frozen=True)
class VadDecision:
    """一帧的判定结果与依据，便于排查「为什么没听到我说话」。"""

    is_speech: bool
    vad_says: bool
    energy: float
    energy_ok: bool

    @property
    def reason(self) -> str:
        if self.is_speech:
            return "人声"
        if self.vad_says and not self.energy_ok:
            return f"频谱像人声但能量不足（{self.energy:.0f}）"
        return "静音"


class VoiceActivityDetector:
    """人声判定：WebRTC VAD **且** 能量达标。"""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 3,
        energy_threshold: float = DEFAULT_ENERGY_THRESHOLD,
    ) -> None:
        if frame_ms not in ALLOWED_FRAME_MS:
            raise ValueError(
                f"帧长必须是 {ALLOWED_FRAME_MS} 之一，收到 {frame_ms}ms"
                "（WebRTC VAD 的硬性要求）"
            )
        if sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError(f"采样率必须是 8k/16k/32k/48k 之一，收到 {sample_rate}")

        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_samples = int(sample_rate * frame_ms / 1000)
        self.frame_bytes = self.frame_samples * 2  # 16bit
        self.energy_threshold = energy_threshold

        # aggressiveness 0~3：越大越激进地判定为「非语音」。
        # 用 3 是因为误触发（把噪声当人声）比漏检更烦人 ——
        # 漏检用户会再说一遍，误触发则是程序自己乱动。
        self._vad = webrtcvad.Vad(aggressiveness)
        log.debug(
            "VAD 就绪：%dHz / %dms / 灵敏度 %d / 能量阈值 %.0f",
            sample_rate, frame_ms, aggressiveness, energy_threshold,
        )

    # ---------- 单帧判定 ----------

    def decide(self, frame: bytes) -> VadDecision:
        """判定单帧，并给出判定依据。

        注意：**同一帧只应调用一次** —— 见模块头部关于内部状态的说明。
        重复调用同一帧会得到不一致的结果。
        """
        if len(frame) != self.frame_bytes:
            # 长度不符的帧一律当静音，而不是抛异常 ——
            # 流式场景下尾帧天然可能不足一帧，为此中断整条链路不值得
            return VadDecision(False, False, 0.0, False)

        try:
            vad_says = self._vad.is_speech(frame, self.sample_rate)
        except Exception as exc:  # webrtcvad 对畸形输入会抛
            log.debug("VAD 判定异常，按静音处理：%s", exc)
            return VadDecision(False, False, 0.0, False)

        energy = self.energy(frame)
        energy_ok = energy >= self.energy_threshold
        return VadDecision(vad_says and energy_ok, vad_says, energy, energy_ok)

    def is_speech(self, frame: bytes) -> bool:
        """便捷入口，只关心结论。"""
        return self.decide(frame).is_speech

    # ---------- 工具 ----------

    @staticmethod
    def energy(frame: bytes) -> float:
        """帧的平均绝对振幅（与 16bit 量程同尺度）。

        用「平均绝对值」而不是 RMS：计算更快，且对突发噪声不那么敏感。
        """
        usable = len(frame) - (len(frame) % 2)
        if usable <= 0:
            return 0.0
        samples = np.frombuffer(frame[:usable], dtype=np.int16)
        if samples.size == 0:
            return 0.0
        return float(np.mean(np.abs(samples.astype(np.float32))))

    def __repr__(self) -> str:
        return (
            f"VoiceActivityDetector({self.sample_rate}Hz, {self.frame_ms}ms, "
            f"energy>={self.energy_threshold:.0f})"
        )
