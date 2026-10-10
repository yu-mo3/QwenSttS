"""L5 -> L6 的线程桥。

## 为什么需要这一层

VoiceAssistant 的事件回调会在 **三个线程** 上被调用：

    ayuan-session   主帧循环（状态流转、识别结果、回复）
    ayuan-input     麦克风输入线程（设备失效时的 error）
    ayuan-speak     播报线程（tts_done / playback_* / interrupted）

而 Qt 规定：**控件只能在 GUI 线程里碰**。跨线程刷控件不是「有时会崩」，
是「迟早会崩」，而且崩在随机位置、随机时刻。

## 怎么做

EventBridge 是一个在 **GUI 线程构造** 的 QObject。工作线程调用
feed_event() 时，Qt 会把这次投递排进 GUI 线程的事件队列
（AutoConnection 因为接收者在 GUI 线程而解析为 QueuedConnection），
槽函数因此在 GUI 线程执行。

## 一条必须遵守的纪律

**槽函数必须连到 QObject 的绑定方法上。**

    window.on_state_changed          # 正确：Qt 知道接收者归属 GUI 线程，排队投递
    lambda old, new: ...             # 错误：没有归属线程，直接在**发射线程**执行

连 lambda 时 Qt 无从判断接收者属于哪个线程，于是退化成直连 ——
槽函数就在 ayuan-speak 线程里跑，然后去刷控件，然后崩。
gui_thread 装饰器把这条纪律变成运行时可检查的断言：违反时立刻带着
方法名抛出可读的错误，而不是留一个随机崩溃给下一个人。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from app.orchestrator.events import (
    EVT_ASR_DONE,
    EVT_ERROR,
    EVT_EXIT,
    EVT_IDLE_TIMEOUT,
    EVT_INTERRUPTED,
    EVT_LLM_DONE,
    EVT_PLAYBACK_DONE,
    EVT_PLAYBACK_START,
    EVT_RETRY,
    EVT_SPEECH_END,
    EVT_SPEECH_START,
    EVT_STATE,
    EVT_TTS_DONE,
    EVT_WAKE,
)

#: 电平来源。界面用它区分「我在说话」和「她在说话」。
SOURCE_MIC = "mic"
SOURCE_SPEAKER = "speaker"


def assert_gui_thread(what: str) -> None:
    """断言当前在 GUI 线程。无 QApplication 时（纯测试）直接放行。"""
    app_cls = _app_class()
    app = app_cls.instance() if app_cls is not None else None
    if app is None:
        return
    current = QThread.currentThread()
    if current is not app.thread():
        raise RuntimeError(
            f"{what} 在非 GUI 线程执行（当前线程 {current.objectName() or current}）—— "
            "槽函数必须连到 QObject 的绑定方法上；连 lambda 会在发射线程执行，"
            "届时刷控件必然崩溃"
        )


def _app_class():
    """延迟取 QApplication 类型。

    不在模块顶层 import QtWidgets：本模块也被纯逻辑测试使用，
    而那些测试不需要（也不该需要）一个 QApplication。
    """
    try:
        from PySide6.QtWidgets import QApplication

        return QApplication
    except Exception:  # pragma: no cover - 只在 QtWidgets 缺失时发生
        return None


def gui_thread(func: Callable[..., Any]) -> Callable[..., Any]:
    """装饰槽函数：确保它在 GUI 线程执行，否则立刻报错。

    用在所有会碰控件的槽上。代价是一次线程比较，
    换来的是「错误的连接方式」在第一次触发时就暴露，而不是随机崩溃。
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        assert_gui_thread(func.__qualname__)
        return func(*args, **kwargs)

    return wrapper


class EventBridge(QObject):
    """把编排层的事件翻译成 Qt 信号。

    **必须在 GUI 线程构造**（继承关系决定了它的线程归属），
    但它的 feed_* 方法可以从任意线程调用。
    """

    #: 原始事件，一个不落地转出去 —— 技术面板的事件日志订阅它。
    #: 排障时最有用的往往正是那些「界面没专门处理」的事件。
    event_received = Signal(str, dict)

    #: 状态变化（旧状态名, 新状态名）
    state_changed = Signal(str, str)

    #: 电平（0~1, "mic" / "speaker"）
    level_changed = Signal(float, str)

    #: 用户的输入确定了（文本, 来源 source）
    user_said = Signal(str, str)

    #: 模型回复就绪（文本, 句子列表, 首字耗时或 None）
    reply_ready = Signal(str, list, object)

    #: 播报进度（第几句, 共几句, 本句音频时长）
    speech_progress = Signal(int, int, float)

    #: 被「停」打断
    interrupted = Signal()

    #: 云端重试提示（第几次, 共几次, 原因）
    retrying = Signal(int, int, str)

    #: 待机超时
    idle_timeout = Signal(float)

    #: 出错（消息, 提示）
    failed = Signal(str, str)

    #: 会话正常结束（统计摘要）—— 由编排层的 exit 事件驱动
    session_finished = Signal(str)

    #: 会话线程彻底退出。与 session_finished 的区别：无论正常结束还是
    #: 中途抛异常，这个都会发。界面靠它恢复按钮状态。
    session_stopped = Signal()

    def feed_event(self, event: str, payload: dict[str, Any]) -> None:
        """从**任意线程**调用。只做信号投递，绝不碰控件。"""
        self.event_received.emit(event, payload)

        if event == EVT_STATE:
            self.state_changed.emit(
                str(payload.get("old", "")), str(payload.get("new", ""))
            )
        elif event == EVT_ASR_DONE:
            self.user_said.emit(
                str(payload.get("text", "")), str(payload.get("source", "speech"))
            )
        elif event == EVT_LLM_DONE:
            self.reply_ready.emit(
                str(payload.get("text", "")),
                list(payload.get("sentences") or []),
                payload.get("first_byte"),
            )
        elif event == EVT_TTS_DONE:
            self.speech_progress.emit(
                int(payload.get("index", 0)),
                int(payload.get("total", 0)),
                float(payload.get("duration", 0.0)),
            )
        elif event == EVT_INTERRUPTED:
            self.interrupted.emit()
        elif event == EVT_RETRY:
            self.retrying.emit(
                int(payload.get("attempt", 0)),
                int(payload.get("total", 0)),
                str(payload.get("reason", "")),
            )
        elif event == EVT_IDLE_TIMEOUT:
            self.idle_timeout.emit(float(payload.get("seconds", 0.0)))
        elif event == EVT_ERROR:
            self.failed.emit(
                str(payload.get("message", "")), str(payload.get("hint", ""))
            )
        elif event == EVT_EXIT:
            self.session_finished.emit(str(payload.get("stats", "")))

    def feed_level(self, level: float, source: str) -> None:
        """从任意线程调用。电平已经在 L5 限频过，这里只管转发。"""
        self.level_changed.emit(float(level), source)


#: 界面「技术面板的事件日志」关心的事件名 —— 全部。
#: 显式列出来是为了让「我们到底订阅了什么」这件事有个唯一答案。
SUBSCRIBED_EVENTS = frozenset(
    {
        EVT_STATE,
        EVT_WAKE,
        EVT_SPEECH_START,
        EVT_SPEECH_END,
        EVT_ASR_DONE,
        EVT_LLM_DONE,
        EVT_TTS_DONE,
        EVT_PLAYBACK_START,
        EVT_PLAYBACK_DONE,
        EVT_INTERRUPTED,
        EVT_RETRY,
        EVT_IDLE_TIMEOUT,
        EVT_ERROR,
        EVT_EXIT,
    }
)
