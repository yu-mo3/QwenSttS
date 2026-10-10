"""L5 编排层的**统一事件契约**。

## 这个模块为什么存在（KI-006）

在 M4 之前，`pipeline.py` 与 `assistant.py` 各自定义了一份事件常量，
其中同名事件的载荷还不一致 —— 最典型的是 `tts_done`：pipeline 发
`{index, total, duration}`，assistant 只发 `{index, duration}`。

这在命令行下无害（各自打印各自的），但 M4 的界面要**统一订阅**这套事件。
只要有一处依赖 `payload['total']`，assistant 那条路径就是必现的 KeyError。
所以收敛不是洁癖，是 M4 的前置条件。

## 契约怎么用

`PAYLOAD_CONTRACTS` 声明每个事件的载荷形状：

    required —— 订阅方可以**无条件**直接取的键
    optional —— 可能存在也可能不存在的键，订阅方必须用 .get()

新增事件或改动载荷时，**同步改这张表** —— `tests/test_events_pure.py`
会用 ast 静态扫描两个编排器的所有 `_emit(...)` 调用，
若实际发出的键与契约不符，测试直接失败。

这样做的原因：事件契约是编排层与表现层之间唯一的口头约定，
而「口头约定」在项目里从来都会腐烂 —— 它必须能被机器检查。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ══════════════════════════════════════════════════════════
#  事件名
# ══════════════════════════════════════════════════════════

# ---- 两个编排器都会发 ----
EVT_ASR_DONE = "asr_done"
EVT_LLM_DONE = "llm_done"
EVT_TTS_DONE = "tts_done"
EVT_PLAYBACK_START = "playback_start"
EVT_PLAYBACK_DONE = "playback_done"
EVT_RETRY = "retry"
EVT_ERROR = "error"

# ---- 常驻助手（VoiceAssistant）专有 ----
EVT_STATE = "state"
EVT_WAKE = "wake"
EVT_SPEECH_START = "speech_start"
EVT_SPEECH_END = "speech_end"
EVT_INTERRUPTED = "interrupted"
EVT_IDLE_TIMEOUT = "idle_timeout"
EVT_EXIT = "exit"

# ---- 单轮流水线（VoicePipeline）专有 ----
EVT_RECORDING_START = "recording_start"
EVT_RECORDING_DONE = "recording_done"


# ══════════════════════════════════════════════════════════
#  载荷契约
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class EventContract:
    """一个事件的载荷形状。"""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


_R = frozenset

PAYLOAD_CONTRACTS: dict[str, EventContract] = {
    # ---- 两个编排器都会发 ----
    EVT_ASR_DONE: EventContract(_R({"text", "emotion", "elapsed"})),
    EVT_LLM_DONE: EventContract(_R({"text", "sentences", "first_byte"})),
    # total 是 KI-006 的核心：M4 的界面靠它显示「第 i / N 句」
    EVT_TTS_DONE: EventContract(_R({"index", "total", "duration"})),
    EVT_PLAYBACK_START: EventContract(_R({"index"})),
    # completed 只有助手会发（pipeline 播完就返回，没有「被打断」这个语义）
    EVT_PLAYBACK_DONE: EventContract(_R({"index"}), _R({"completed"})),
    EVT_RETRY: EventContract(_R({"attempt", "total", "reason"})),
    EVT_ERROR: EventContract(_R({"message", "hint"})),

    # ---- 常驻助手专有 ----
    EVT_STATE: EventContract(_R({"old", "new"})),
    EVT_WAKE: EventContract(),
    EVT_SPEECH_START: EventContract(),
    EVT_SPEECH_END: EventContract(_R({"duration"})),
    EVT_INTERRUPTED: EventContract(),
    EVT_IDLE_TIMEOUT: EventContract(_R({"seconds"})),
    # pipeline 不发 exit；助手的 exit 带一句会话小结
    EVT_EXIT: EventContract(_R(), _R({"stats"})),

    # ---- 单轮流水线专有 ----
    EVT_RECORDING_START: EventContract(_R({"seconds"})),
    EVT_RECORDING_DONE: EventContract(_R({"bytes"})),
}

#: 所有合法事件名。用于校验，也方便界面做「只订阅这几个」的白名单。
ALL_EVENTS: frozenset[str] = frozenset(PAYLOAD_CONTRACTS)


def missing_keys(event: str, payload: dict[str, Any]) -> list[str]:
    """返回载荷里**缺失的必需键**。空列表表示合规。

    未知事件返回空列表 —— 契约不是白名单，
    发一个契约表里没有的事件不该让订阅方收不到消息。
    """
    contract = PAYLOAD_CONTRACTS.get(event)
    if contract is None:
        return []
    return sorted(contract.required - payload.keys())


def unexpected_keys(event: str, payload: dict[str, Any]) -> list[str]:
    """返回载荷里**契约之外的多余键**。空列表表示合规。

    比缺键更宽松地对待：多余键不会让订阅方崩溃，
    但通常意味着有人改了 `_emit` 却忘了改契约表。
    """
    contract = PAYLOAD_CONTRACTS.get(event)
    if contract is None:
        return []
    return sorted(payload.keys() - contract.allowed)


#: 事件回调的签名：(事件名, 载荷)。
#: 订阅方**不得**假设它运行在某个特定线程上 —— 见 assistant.py 的线程模型。
EventCallback = Callable[[str, dict[str, Any]], None]


# ══════════════════════════════════════════════════════════
#  一轮对话的结果
# ══════════════════════════════════════════════════════════

#: 计时的展示顺序。两个编排器的键是它的子集，因此可以共用一份实现：
#:     pipeline  -> 识别 / 对话 / 合成 / 播放
#:     assistant -> 识别 / 对话 / 播报
TIMING_ORDER: tuple[str, ...] = ("识别", "对话", "合成", "播报", "播放")

#: 旧版本会往 timings 里塞这个键，而 timing_summary() 又把所有值求和 ——
#: 结果「合计」被算了两次。保留这个常量是为了**显式地把它排除掉**，
#: 并让改回来的成本变得很高。
_LEGACY_TOTAL_KEY = "合计"


@dataclass
class TurnResult:
    """一轮对话的完整结果。

    这是 pipeline 与 assistant 共用的超集：pipeline 用不到
    `played` / `interrupted`，assistant 用不到 `sentences` / `emotion`，
    但两者的字段语义完全一致，拆成两份只会让订阅方要写两个分支。

    **合计从不存储，永远现算** —— 存了就会和分项脱节
    （M2 审查 B2 修过一次，assistant 里又原样犯了一次）。
    """

    user_text: str = ""
    reply_text: str = ""
    sentences: list[str] = field(default_factory=list)
    emotion: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    audio_seconds: float = 0.0
    played: int = 0
    interrupted: bool = False
    failed: bool = False
    error: str = ""

    def timing_summary(self) -> str:
        """一行计时摘要。没有分项时返回空串。"""
        parts = [
            f"{k} {self.timings[k]:.2f}s" for k in TIMING_ORDER if k in self.timings
        ]
        if not parts:
            return ""
        # 排除 _LEGACY_TOTAL_KEY：它若存在，说明有调用方还在存合计，
        # 直接求和会把它加进去，正是要修的那个矛盾
        total = sum(v for k, v in self.timings.items() if k != _LEGACY_TOTAL_KEY)
        parts.append(f"合计 {total:.2f}s")
        return " · ".join(parts)
