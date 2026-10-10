"""助手的状态定义与转换规则。

状态机的价值不在于状态本身，而在于**把「现在能不能做某事」
变成一个有唯一答案的问题** —— 而不是散落在各处的布尔标志。

    IDLE      待机：只在等唤醒词，麦克风的音频只喂给唤醒检测
    LISTENING 聆听：已被唤醒，在等用户把话说完（VAD 判定结束）
    THINKING  思考：已识别出用户的话，在等模型回复
    SPEAKING  说话：正在播报，此时监听「停」指令
    EXITING   退出：收到退出指令，收尾中

允许的转换（写成表是为了让非法转换能被拒绝，而不是静默发生）：

    IDLE      -> LISTENING          检测到唤醒词
    IDLE      -> THINKING           收到打字输入（M4：不经过唤醒与聆听）
    LISTENING -> THINKING           语音段结束且识别出内容
    LISTENING -> IDLE               超时没人说话 / 没识别出内容
    THINKING  -> SPEAKING           拿到回复
    THINKING  -> IDLE               模型失败或回复为空
    SPEAKING  -> LISTENING          回复说完，进入连续对话
    SPEAKING  -> IDLE               说「再见」，或播报被中断
    *         -> EXITING            任何时候收到退出指令
"""

from __future__ import annotations

from enum import Enum

from app.core.errors import AyuanError


class AssistantState(str, Enum):
    """助手状态。继承 str 是为了让它能直接进日志和事件载荷。"""

    IDLE = "待机"
    LISTENING = "聆听"
    THINKING = "思考"
    SPEAKING = "说话"
    EXITING = "退出"

    def __str__(self) -> str:
        return self.value


#: 允许的状态转换。任何不在此表中的转换都会被拒绝并打日志，
#: 因为非法转换通常意味着代码里有并发 bug，而不是"碰巧发生"。
ALLOWED_TRANSITIONS: dict[AssistantState, set[AssistantState]] = {
    AssistantState.IDLE: {
        AssistantState.LISTENING,
        # M4 新增：打字输入是一条**不经过唤醒与聆听**的入口。
        # 原先这里没有它，是因为在只有语音入口时「待机直接到思考」
        # 确实不可能发生 —— 那是把实现现状当成了规则。
        # 注意 IDLE -> SPEAKING 仍然非法：没有思考就没有话可说。
        AssistantState.THINKING,
        AssistantState.EXITING,
    },
    AssistantState.LISTENING: {
        AssistantState.THINKING,
        AssistantState.IDLE,
        AssistantState.EXITING,
    },
    AssistantState.THINKING: {
        AssistantState.SPEAKING,
        AssistantState.IDLE,
        AssistantState.EXITING,
    },
    AssistantState.SPEAKING: {
        AssistantState.LISTENING,
        AssistantState.IDLE,
        AssistantState.EXITING,
    },
    AssistantState.EXITING: set(),
}


def can_transition(src: AssistantState, dst: AssistantState) -> bool:
    """判断转换是否合法。"""
    if src == dst:
        return True
    return dst in ALLOWED_TRANSITIONS.get(src, set())


class InvalidTransition(AyuanError):
    """非法状态转换。

    继承 AyuanError 而不是裸 RuntimeError（M4 修 KI-007①）：
    它原先不属于异常家族，一旦从 run() 的 try 之外抛出，上层的
    `except AyuanError` 兜不住，用户看到的就是一段 traceback。
    """

    def __init__(self, src: AssistantState, dst: AssistantState) -> None:
        super().__init__(
            f"非法状态转换：{src} -> {dst}",
            hint="这通常意味着状态流转出了并发问题，而不是用户操作导致的",
        )
        self.src = src
        self.dst = dst
