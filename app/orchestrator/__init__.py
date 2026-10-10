"""L5 编排层：把「能力」编排成「体验」。

两个组件，分工不同：

    VoicePipeline  —— 一轮对话的**执行者**，无状态（M2）
    VoiceAssistant —— 一次会话的**持有者**，管状态流转、连续对话、打断（M3）

状态机定义在 states.py，非法转换会被拒绝而不是静默发生。
"""

from app.orchestrator.assistant import (
    EVT_ASR_DONE,
    EVT_ERROR,
    EVT_EXIT,
    EVT_IDLE_TIMEOUT,
    EVT_INTERRUPTED,
    EVT_LLM_DONE,
    EVT_PLAYBACK_START,
    EVT_RETRY,
    EVT_SPEECH_END,
    EVT_STATE,
    EVT_WAKE,
    SessionStats,
    VoiceAssistant,
)
from app.orchestrator.pipeline import EventCallback, TurnResult, VoicePipeline
from app.orchestrator.states import (
    ALLOWED_TRANSITIONS,
    AssistantState,
    InvalidTransition,
    can_transition,
)

__all__ = [
    # 单轮流水线
    "VoicePipeline",
    "TurnResult",
    "EventCallback",
    # 常驻助手
    "VoiceAssistant",
    "SessionStats",
    # 状态机
    "AssistantState",
    "ALLOWED_TRANSITIONS",
    "can_transition",
    "InvalidTransition",
    # 事件名
    "EVT_STATE",
    "EVT_WAKE",
    "EVT_SPEECH_END",
    "EVT_ASR_DONE",
    "EVT_LLM_DONE",
    "EVT_PLAYBACK_START",
    "EVT_INTERRUPTED",
    "EVT_RETRY",
    "EVT_IDLE_TIMEOUT",
    "EVT_ERROR",
    "EVT_EXIT",
]
