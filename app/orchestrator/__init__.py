"""L5 编排层：把「能力」编排成「体验」。

M2 阶段只有一条最朴素的流水线（录音→识别→对话→合成→播放）。
M3 会升级为状态机：待机 / 唤醒 / 聆听 / 思考 / 说话 / 被打断，
并引入 VAD 断句与随时打断。
"""

from app.orchestrator.pipeline import EventCallback, TurnResult, VoicePipeline

__all__ = ["VoicePipeline", "TurnResult", "EventCallback"]
