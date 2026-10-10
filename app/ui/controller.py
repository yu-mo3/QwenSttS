"""会话控制器：在后台线程里持有 VoiceAssistant。

这是 L6 里**唯一**认识 L5 的类。控件只连它的信号、只调它的方法，
不 import 编排层也不 import 任何能力层 —— 「界面不认识任何模型」
这条铁律靠的就是这个边界。

## 为什么会话要跑在后台线程

VoiceAssistant.run() 是一个永不返回的帧循环（直到收到退出指令）。
把它放在 GUI 线程里，窗口会直接失去响应 —— 而「界面卡死」正是这个项目
从一开始就想避免的事（见 README 里与原教程的对照表）。
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject

from app.config import Config
from app.core.errors import AyuanError
from app.core.logger import get_logger
from app.orchestrator.assistant import VoiceAssistant
from app.orchestrator.events import (
    CMD_LISTEN,
    CMD_RESET,
    CMD_SHUTDOWN,
    CMD_STOP_SPEAKING,
)
from app.ui.bridge import EventBridge

log = get_logger(__name__)

#: 退出时等待会话线程收尾的上限（秒）。
#: 到点还没结束就放弃等待 —— 界面必须能关掉，哪怕后台线程有问题。
STOP_TIMEOUT = 3.0


class SessionController(QObject):
    """持有一次语音会话。必须在 GUI 线程构造。"""

    def __init__(
        self,
        config: Config,
        *,
        idle_timeout: float = 30.0,
        wake_threshold: float | None = None,
        stop_threshold: float | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.idle_timeout = idle_timeout
        self.wake_threshold = wake_threshold
        self.stop_threshold = stop_threshold

        # 桥在这里构造 —— QObject 的线程归属就是构造它的线程，
        # 所以这一行必须发生在 GUI 线程里
        self.bridge = EventBridge(self)

        self._assistant: VoiceAssistant | None = None
        self._thread: threading.Thread | None = None

    # ══════════════════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════════════════

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        """开一次会话。已经在跑时返回 False。"""
        if self.is_running:
            log.debug("会话已在运行，忽略重复的 start()")
            return False

        # 构造是廉价的：真正打开麦克风发生在 run() 里，
        # 因此这里失败（缺模型、缺设备）会通过事件报给界面，而不是抛在 GUI 线程
        self._assistant = VoiceAssistant(
            self.config,
            on_event=self.bridge.feed_event,
            on_level=self.bridge.feed_level,
            idle_timeout=self.idle_timeout,
            wake_threshold=self.wake_threshold,
            stop_threshold=self.stop_threshold,
        )
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="ayuan-session"
        )
        self._thread.start()
        log.info("会话已启动")
        return True

    def _run(self) -> None:
        assistant = self._assistant
        try:
            assistant.run()  # type: ignore[union-attr]
        except AyuanError as exc:
            # 项目异常带着给用户看的 hint，原样转给界面
            self.bridge.failed.emit(str(exc), exc.hint)
        except Exception as exc:  # noqa: BLE001 — 最后一道防线
            log.exception("会话线程出现未预料的异常")
            self.bridge.failed.emit(
                f"{type(exc).__name__}: {exc}",
                "这是未预料的错误。技术面板里的事件日志能帮助定位问题",
            )
        finally:
            self._thread = None
            self.bridge.session_stopped.emit()
            log.info("会话线程已退出")

    def stop(self) -> None:
        """请求结束会话。立即返回，不等线程退出。"""
        assistant = self._assistant
        if assistant is not None:
            assistant.stop()

    def wait_stopped(self, timeout: float = STOP_TIMEOUT) -> bool:
        """等会话线程退出。返回是否已退出。"""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        alive = thread.is_alive()
        if alive:
            log.warning("会话线程在 %.1fs 内没有退出，放弃等待", timeout)
        return not alive

    def restart(self) -> None:
        """结束当前会话并重开一个（托盘菜单与「应用设备设置」用）。

        设备变更必须走这条路：麦克风与扬声器只在 run() 开始时打开一次，
        换设备只能重开会话，没有第二条路。
        """
        self.stop()
        self.wait_stopped()
        self.start()

    # ══════════════════════════════════════════════════════
    #  指令（全部可以从 GUI 线程直接调）
    # ══════════════════════════════════════════════════════

    def _post(self, command: str) -> None:
        assistant = self._assistant
        if assistant is not None:
            assistant.post(command)

    def listen_now(self) -> None:
        """「点击说话」：绕过唤醒词直接进入聆听。"""
        self._post(CMD_LISTEN)

    def stop_speaking(self) -> None:
        self._post(CMD_STOP_SPEAKING)

    def reset_turn(self) -> None:
        self._post(CMD_RESET)

    def shutdown(self) -> None:
        """走指令队列退出 —— 让主循环自己收尾，与点「停止」等价但更有序。"""
        self._post(CMD_SHUTDOWN)

    def submit_text(self, text: str) -> bool:
        assistant = self._assistant
        if assistant is None:
            return False
        return assistant.submit_text(text)

    def clear_memory(self) -> None:
        assistant = self._assistant
        if assistant is not None:
            assistant.clear_memory()

    # ══════════════════════════════════════════════════════
    #  运行中改设置
    # ══════════════════════════════════════════════════════

    def set_idle_timeout(self, seconds: float) -> None:
        """改静默超时。纯数值，就地生效。"""
        seconds = max(1.0, float(seconds))
        self.idle_timeout = seconds
        assistant = self._assistant
        if assistant is not None:
            assistant.set_idle_timeout(seconds)
        log.info("静默超时改为 %.0f 秒", seconds)

    def set_thresholds(
        self, *, wake: float | None = None, stop: float | None = None
    ) -> None:
        """改唤醒/停阈值。下一个音频帧起生效。"""
        if wake is not None:
            self.wake_threshold = float(wake)
        if stop is not None:
            self.stop_threshold = float(stop)
        assistant = self._assistant
        if assistant is not None:
            assistant.set_thresholds(wake=wake, stop=stop)
