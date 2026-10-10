"""主窗口：脸谱式光球 + 对话历史 + 技术面板。

## 布局

    ┌────────────────────────────────────────────────────┐
    │  阿源                                    ─  □  ×   │
    ├──────────────────────┬─────────────────────────────┤
    │   [Panel]            │   [Panel]                   │
    │     光球              │     对话历史                 │
    │     状态 / 提示        │                             │
    │     电平条            │                             │
    ├──────────────────────┴─────────────────────────────┤
    │  [🎙 点击说话] [✋ 停] [输入框........] [发送] [⚙]   │
    ├────────────────────────────────────────────────────┤
    │  ▸ 技术面板（可折叠：电平波形 / 设备 / 参数 / 事件）  │
    └────────────────────────────────────────────────────┘

## 一条纪律

**所有槽函数都连成本窗口的绑定方法**，并且都带 @gui_thread 装饰器。
连 lambda 会在发射线程执行，届时刷控件必然崩溃 ——
bridge.py 里讲了原因，gui_thread 把它变成第一次触发就报错的断言。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.core.logger import get_logger
from app.orchestrator.states import AssistantState
from app.ui import theme
from app.ui.bridge import SOURCE_MIC, gui_thread
from app.ui.controller import SessionController
from app.ui.history import HistoryPanel
from app.ui.orb import OrbWidget

log = get_logger(__name__)

#: 事件日志最多保留多少行。排障看最近几百条足够，
#: 无限增长会让一个常驻一整天的助手把内存吃光。
EVENT_LOG_LIMIT = 500

#: 电平条的格子数。20 格在 380px 的栏宽里刚好够看清又不占地方。
_BAR_CELLS = 20


def _bar(level: float) -> str:
    """把 0~1 的电平画成一行方块。纯函数，方便测。"""
    filled = int(round(min(max(level, 0.0), 1.0) * _BAR_CELLS))
    return "█" * filled + "·" * (_BAR_CELLS - filled)


class MainWindow(QMainWindow):
    """阿源的主窗口。"""

    def __init__(self, config: Config, controller: SessionController) -> None:
        super().__init__()
        self.config = config
        self.controller = controller
        self.bridge = controller.bridge

        self._state = AssistantState.IDLE
        self._mic_level = 0.0
        self._speaker_level = 0.0
        self._closing = False

        self.setWindowTitle("阿源 · QwenSttS")
        self.resize(theme.WINDOW_DEFAULT_WIDTH, theme.WINDOW_DEFAULT_HEIGHT)
        self.setMinimumSize(theme.WINDOW_MIN_WIDTH, theme.WINDOW_MIN_HEIGHT)

        self._build_ui()
        self._connect()
        self._apply_state(AssistantState.IDLE)

    # ══════════════════════════════════════════════════════
    #  装配
    # ══════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        outer.setSpacing(theme.GAP)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_stage_column())
        splitter.addWidget(self._build_history_column())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([theme.ORB_COLUMN_WIDTH, 600])
        outer.addWidget(splitter, 1)

        outer.addWidget(self._build_input_row())

        self._tech_toggle = QPushButton("▸ 技术面板")
        self._tech_toggle.setCheckable(True)
        self._tech_toggle.setObjectName("TechToggle")
        self._tech_toggle.toggled.connect(self.on_tech_toggled)
        outer.addWidget(self._tech_toggle)

        self._tech_body = self._build_tech_panel()
        self._tech_body.setVisible(False)
        outer.addWidget(self._tech_body)

    def _build_stage_column(self) -> QWidget:
        """左栏：脸谱式光球 + 状态文字。"""
        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setFixedWidth(theme.ORB_COLUMN_WIDTH)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        layout.setSpacing(theme.GAP)

        self.orb = OrbWidget()
        layout.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignHCenter)

        self.state_label = QLabel()
        self.state_label.setObjectName("StateLabel")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.state_label)

        self.hint_label = QLabel()
        self.hint_label.setObjectName("Dim")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.level_label = QLabel()
        self.level_label.setObjectName("Dim")
        self.level_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.level_label)

        layout.addStretch(1)
        return panel

    def _build_history_column(self) -> QWidget:
        """右栏：对话历史。"""
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        layout.setSpacing(theme.GAP)

        title = QLabel("对话历史")
        title.setObjectName("Title")
        layout.addWidget(title)

        self.history = HistoryPanel()
        layout.addWidget(self.history, 1)
        return panel

    def _build_input_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.GAP)

        self.listen_button = QPushButton("🎙 点击说话")
        self.listen_button.setObjectName("Primary")
        self.listen_button.setToolTip("不用等唤醒词，直接开始听你说")
        layout.addWidget(self.listen_button)

        self.stop_button = QPushButton("✋ 停")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setToolTip("让她立刻停止播报")
        layout.addWidget(self.stop_button)

        self.input = QLineEdit()
        self.input.setPlaceholderText("打字和阿源说话，回车发送…")
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self.on_input_changed)
        layout.addWidget(self.input, 1)

        self.send_button = QPushButton("发送")
        self.send_button.setEnabled(False)   # 空输入时发送没有意义
        layout.addWidget(self.send_button)

        self.settings_button = QPushButton("⚙ 设置")
        layout.addWidget(self.settings_button)
        return row

    def _build_tech_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        layout.setSpacing(theme.GAP)

        caption = QLabel("事件日志（最近 %d 条）" % EVENT_LOG_LIMIT)
        caption.setObjectName("Dim")
        layout.addWidget(caption)

        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumBlockCount(EVENT_LOG_LIMIT)
        self.event_log.setFixedHeight(180)
        layout.addWidget(self.event_log)
        return panel

    # ══════════════════════════════════════════════════════
    #  信号连接 —— 全部连到本窗口的绑定方法（见模块文档）
    # ══════════════════════════════════════════════════════

    def _connect(self) -> None:
        b = self.bridge
        b.state_changed.connect(self.on_state_changed)
        b.level_changed.connect(self.on_level_changed)
        b.user_said.connect(self.on_user_said)
        b.reply_ready.connect(self.on_reply_ready)
        b.speech_progress.connect(self.on_speech_progress)
        b.interrupted.connect(self.on_interrupted)
        b.retrying.connect(self.on_retrying)
        b.idle_timeout.connect(self.on_idle_timeout)
        b.failed.connect(self.on_failed)
        b.session_finished.connect(self.on_session_finished)
        b.session_stopped.connect(self.on_session_stopped)
        b.event_received.connect(self.on_event_received)

        self.listen_button.clicked.connect(self.on_listen_clicked)
        self.stop_button.clicked.connect(self.on_stop_clicked)
        self.input.returnPressed.connect(self.on_send_clicked)
        self.send_button.clicked.connect(self.on_send_clicked)

    # ══════════════════════════════════════════════════════
    #  槽函数
    # ══════════════════════════════════════════════════════

    @gui_thread
    def on_state_changed(self, old: str, new: str) -> None:
        try:
            state = AssistantState(new)
        except ValueError:
            log.warning("收到了未知状态名：%r", new)
            return
        self._state = state
        self._apply_state(state)

    def _apply_state(self, state: AssistantState) -> None:
        style = theme.style_for(state)
        self.state_label.setText(style.label)
        self.state_label.setStyleSheet("color: " + style.core + ";")
        self.hint_label.setText(style.hint)
        self.stop_button.setEnabled(state is AssistantState.SPEAKING)
        self.orb.set_state(state)
        # 状态换了，驱动的电平来源也跟着换：聆听看麦克风、说话看播放
        self._push_orb_level()

    @gui_thread
    def on_level_changed(self, level: float, source: str) -> None:
        if source == SOURCE_MIC:
            self._mic_level = level
        else:
            self._speaker_level = level
        self._push_orb_level()
        self.level_label.setText(
            f"🎤 {_bar(self._mic_level)}   🔊 {_bar(self._speaker_level)}"
        )

    def _push_orb_level(self) -> None:
        """把**当前状态对应的那一路**电平喂给光球。

        聆听态要让球跟着用户的声音胀缩（他在说，球在回应他），
        说话态要跟着播放音量脉动（她真的在出声）。
        拿错来源的话，球会对着一个与当下无关的信号乱动。
        """
        if self._state is AssistantState.SPEAKING:
            self.orb.set_level(self._speaker_level)
        else:
            self.orb.set_level(self._mic_level)

    @gui_thread
    def on_user_said(self, text: str, source: str) -> None:
        self.history.add_user(text, source)

    @gui_thread
    def on_reply_ready(self, text: str, sentences: list, first_byte: object) -> None:
        self.history.add_assistant(text, first_byte)

    @gui_thread
    def on_speech_progress(self, index: int, total: int, duration: float) -> None:
        log.debug("播报第 %d/%d 句，%.1fs", index, total, duration)

    @gui_thread
    def on_interrupted(self) -> None:
        self.history.add_note("被打断")

    @gui_thread
    def on_retrying(self, attempt: int, total: int, reason: str) -> None:
        self.history.add_note(f"云端没响应，重试 {attempt}/{total}")

    @gui_thread
    def on_idle_timeout(self, seconds: float) -> None:
        self.history.add_note(f"静默 {seconds:.0f} 秒，回到待机")

    @gui_thread
    def on_failed(self, message: str, hint: str) -> None:
        # 错误必须落到历史里，不能只写日志 —— 界面上看不见就等于没发生
        self.history.add_note(f"❌ {message}")
        if hint:
            self.history.add_note(hint)
        log.error("会话出错：%s（%s）", message, hint)

    @gui_thread
    def on_session_finished(self, stats: str) -> None:
        if stats:
            self.history.add_note(f"会话结束：{stats}")

    @gui_thread
    def on_session_stopped(self) -> None:
        self.listen_button.setEnabled(True)

    @gui_thread
    def on_event_received(self, event: str, payload: dict) -> None:
        """技术面板的事件日志。

        不解析、不美化，原样打一行 —— 排障时最有用的是**原始载荷**，
        而不是别人替你总结过的版本。
        """
        summary = ", ".join(f"{k}={v!r}" for k, v in payload.items())
        self.event_log.appendPlainText(f"{event:<16} {summary}")

    # ══════════════════════════════════════════════════════
    #  按钮
    # ══════════════════════════════════════════════════════

    @gui_thread
    def on_listen_clicked(self) -> None:
        if not self.controller.is_running:
            self.controller.start()
            # 会话刚起步，唤醒器还在加载；这条指令会在主循环起来后被处理
        self.controller.listen_now()

    @gui_thread
    def on_stop_clicked(self) -> None:
        self.controller.stop_speaking()

    @gui_thread
    def on_send_clicked(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        if not self.controller.is_running:
            self.controller.start()

        if not self.controller.submit_text(text):
            self.history.add_note("没送出去：会话还没就绪，稍等一下再试")
            return

        self.input.clear()
        # 这里**不能**自己往历史里加一条 —— 打字输入会走完整的一轮，
        # 编排层随后会发 asr_done(source=typed)，on_user_said 再渲染一次。
        # 两边都写就会出现两个一模一样的用户气泡（M4-5 修）。

    @gui_thread
    def on_input_changed(self, text: str) -> None:
        self.send_button.setEnabled(bool(text.strip()))

    @gui_thread
    def on_tech_toggled(self, shown: bool) -> None:
        self._tech_body.setVisible(shown)
        self._tech_toggle.setText(("▾ " if shown else "▸ ") + "技术面板")

    # ══════════════════════════════════════════════════════
    #  关闭
    # ══════════════════════════════════════════════════════

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt 的命名
        if self._closing:
            event.accept()
            return

        answer = QMessageBox.question(
            self,
            "退出阿源？",
            "要退出阿源吗？退出后麦克风会立即归还给系统。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self._closing = True
        event.accept()

    def request_shutdown(self) -> None:
        """外部（托盘）要求退出时走这条路，跳过确认框。"""
        self._closing = True
        self.close()
