"""L5 编排层：把「能力」编排成「体验」。

两个组件，分工不同：

    VoicePipeline  —— 一轮对话的**执行者**，无状态（M2）
    VoiceAssistant —— 一次会话的**持有者**，管状态流转、连续对话、打断（M3）

状态机定义在 states.py，非法转换会被拒绝而不是静默发生。
事件契约定义在 events.py，两个编排器共用同一份（M4 修 KI-006）。
"""

from app.orchestrator.assistant import SessionStats, VoiceAssistant
from app.orchestrator.events import (
    ALL_EVENTS,
    PAYLOAD_CONTRACTS,
    EVT_ASR_DONE,
    EVT_ERROR,
    EVT_EXIT,
    EVT_IDLE_TIMEOUT,
    EVT_INTERRUPTED,
    EVT_LLM_DONE,
    EVT_PLAYBACK_DONE,
    EVT_PLAYBACK_START,
    EVT_RECORDING_DONE,
    EVT_RECORDING_START,
    EVT_RETRY,
    EVT_SPEECH_END,
    EVT_SPEECH_START,
    EVT_STATE,
    EVT_TTS_DONE,
    EVT_WAKE,
    EventCallback,
    EventContract,
    TurnResult,
    missing_keys,
)
from app.orchestrator.pipeline import VoicePipeline
from app.orchestrator.states import (
    ALLOWED_TRANSITIONS,
    AssistantState,
    InvalidTransition,
    can_transition,
)

__all__ = [
    # 单轮流水线
    "VoicePipeline",
    # 常驻助手
    "VoiceAssistant",
    "SessionStats",
    # 状态机
    "AssistantState",
    "ALLOWED_TRANSITIONS",
    "can_transition",
    "InvalidTransition",
    # 事件契约（M4 起为两个编排器共用）
    "TurnResult",
    "EventCallback",
    "EventContract",
    "PAYLOAD_CONTRACTS",
    "ALL_EVENTS",
    "missing_keys",
    "EVT_STATE",
    "EVT_WAKE",
    "EVT_SPEECH_START",
    "EVT_SPEECH_END",
    "EVT_ASR_DONE",
    "EVT_LLM_DONE",
    "EVT_TTS_DONE",
    "EVT_PLAYBACK_START",
    "EVT_PLAYBACK_DONE",
    "EVT_INTERRUPTED",
    "EVT_RETRY",
    "EVT_IDLE_TIMEOUT",
    "EVT_ERROR",
    "EVT_EXIT",
    "EVT_RECORDING_START",
    "EVT_RECORDING_DONE",
]
