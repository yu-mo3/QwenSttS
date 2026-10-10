"""系统托盘图标。

常驻助手待在托盘里比占着任务栏自然，但**必须**有一条明确的退出路径 ——
否则用户只能去任务管理器里杀进程，而那时麦克风还占着。

## 图标是画出来的

项目没有美术素材，而图标正好该跟着状态变色（待机青、聆听绿、思考琥珀、
说话紫），复用光球的配色即可 —— 不用多带一个 .ico 文件，
也不会出现「图标和界面对不上色」这种事。

## 托盘不可用时必须退化

Windows 上托盘图标可能因为系统设置或资源管理器异常而创建失败。
那时如果还把窗口藏起来，用户就再也找不回来了 ——
所以「收进托盘」在托盘不可用时会被禁用，而不是静默失效。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QRadialGradient
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from app.core.logger import get_logger
from app.orchestrator.states import AssistantState
from app.ui import theme

log = get_logger(__name__)

ICON_SIZE = 64


def make_icon(state: AssistantState, size: int = ICON_SIZE) -> QIcon:
    """按状态画一个托盘图标。纯绘制，不依赖任何素材文件。"""
    style = theme.style_for(state)
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    center = size / 2.0
    radius = size * 0.34

    glow = QRadialGradient(center, center, center)
    glow.setColorAt(0.0, QColor(style.glow))
    glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(glow)
    painter.drawEllipse(0, 0, size, size)

    core = QRadialGradient(center - radius * 0.3, center - radius * 0.3, radius * 1.5)
    core.setColorAt(0.0, QColor(style.core).lighter(130))
    core.setColorAt(1.0, QColor(style.core).darker(130))
    painter.setBrush(core)
    painter.drawEllipse(int(center - radius), int(center - radius), int(radius * 2), int(radius * 2))

    painter.end()
    return QIcon(pixmap)


class AyuanTray(QObject):
    """托盘图标与菜单。"""

    def __init__(self, window, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.window = window

        self.icon = QSystemTrayIcon(make_icon(AssistantState.IDLE), self)
        self.icon.setToolTip("阿源 · 待机")

        menu = QMenu()
        self.show_action = QAction("显示窗口", menu)
        self.show_action.triggered.connect(self.on_toggle_window)
        menu.addAction(self.show_action)
        menu.addSeparator()

        self.restart_action = QAction("重新开始会话", menu)
        self.restart_action.setToolTip("换过设备或会话卡住时用")
        self.restart_action.triggered.connect(self.on_restart)
        menu.addAction(self.restart_action)

        self.forget_action = QAction("清空对话记忆", menu)
        self.forget_action.setToolTip("她会忘掉刚才聊过什么，但状态不受影响")
        self.forget_action.triggered.connect(self.on_forget)
        menu.addAction(self.forget_action)

        self.stop_action = QAction("停止播报", menu)
        self.stop_action.triggered.connect(self.on_stop_speaking)
        menu.addAction(self.stop_action)
        menu.addSeparator()

        quit_action = QAction("退出阿源", menu)
        quit_action.triggered.connect(self.on_quit)
        menu.addAction(quit_action)

        self.icon.setContextMenu(menu)
        self.menu = menu

        self.icon.activated.connect(self.on_activated)
        window.bridge.state_changed.connect(self.on_state_changed)

    # ---------- 可用性 ----------

    @property
    def available(self) -> bool:
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self) -> bool:
        """挂上托盘。返回是否成功 —— 失败时调用方要保证窗口还看得见。"""
        if not self.available:
            log.warning("系统托盘不可用，「收进托盘」这条路会退化")
            return False
        self.icon.show()
        return True

    # ---------- 槽 ----------

    def on_state_changed(self, old: str, new: str) -> None:
        try:
            state = AssistantState(new)
        except ValueError:
            return
        self.icon.setIcon(make_icon(state))
        self.icon.setToolTip(f"阿源 · {theme.style_for(state).label}")

    def on_activated(self, reason) -> None:
        # 双击（或单击，取决于桌面环境）就把窗口叫回来
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.on_toggle_window()

    def on_toggle_window(self) -> None:
        window: QWidget = self.window
        if window.isVisible() and not window.isMinimized():
            window.hide()
            self.show_action.setText("显示窗口")
        else:
            window.showNormal()
            window.raise_()
            window.activateWindow()
            self.show_action.setText("隐藏窗口")

    def on_restart(self) -> None:
        log.info("托盘：重新开始会话")
        self.window.controller.restart()

    def on_forget(self) -> None:
        log.info("托盘：清空对话记忆")
        self.window.controller.clear_memory()
        self.window.history.clear()
        self.window.history.add_note("对话记忆已清空")

    def on_stop_speaking(self) -> None:
        self.window.controller.stop_speaking()

    def on_quit(self) -> None:
        self.window.request_shutdown()
