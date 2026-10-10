"""技术面板：电平波形 / 设备 / 参数 / 事件日志。

默认折叠。日常使用不需要看见它们，但一旦出问题，这一屏就是
「截一张图发给开发者」的那张图 —— 所以它显示的必须是**原始值**，
而不是替你美化过的摘要。

## 波形为什么要自己存一份历史

L5 每次只报一个瞬时电平。曲线得自己攒 —— LevelTrace 是一个纯 Python
的环形缓冲，不依赖 Qt，因此「攒了多少点、丢了最旧的没有」可以被单测锁住。
"""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme
from app.ui.bridge import SOURCE_MIC, SOURCE_SPEAKER

#: 波形保留多少个采样点。L5 约 16Hz 上报，300 点 ≈ 18 秒。
TRACE_POINTS = 300

#: 事件日志最多保留多少行。排障看最近几百条足够，
#: 无限增长会让一个常驻一整天的助手把内存吃光。
EVENT_LOG_LIMIT = 500


class LevelTrace:
    """两条电平曲线（麦克风 / 扬声器）的环形缓冲。**纯 Python，可测**。"""

    def __init__(self, points: int = TRACE_POINTS) -> None:
        self.points = max(1, int(points))
        self._mic: deque[float] = deque(maxlen=self.points)
        self._speaker: deque[float] = deque(maxlen=self.points)

    def push(self, level: float, source: str) -> None:
        try:
            value = float(level)
        except (TypeError, ValueError):
            return
        value = min(max(value, 0.0), 1.0)
        if source == SOURCE_MIC:
            self._mic.append(value)
        elif source == SOURCE_SPEAKER:
            self._speaker.append(value)
        # 未知来源直接丢掉：把麦克风的电平画进播放曲线，
        # 会让看图的人得出完全错误的结论，比少一条数据糟得多

    def series(self, source: str) -> tuple[float, ...]:
        return tuple(self._mic if source == SOURCE_MIC else self._speaker)

    def clear(self) -> None:
        self._mic.clear()
        self._speaker.clear()

    @property
    def mic(self) -> tuple[float, ...]:
        return tuple(self._mic)

    @property
    def speaker(self) -> tuple[float, ...]:
        return tuple(self._speaker)


class WaveformWidget(QWidget):
    """把 LevelTrace 画成两条滚动曲线。"""

    def __init__(self, trace: LevelTrace, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.trace = trace
        self.setMinimumHeight(70)
        self.setFixedHeight(88)

    def refresh(self) -> None:
        self.update()

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt 的命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        width = self.width()
        height = self.height()

        # 底色 + 两条参考线（中线与顶线），便于判断音量大小
        painter.fillRect(0, 0, width, height, QColor(theme.DARK.panel_alt))
        grid = QColor(theme.DARK.border)
        painter.setPen(QPen(grid, 1))
        for ratio in (0.25, 0.5, 0.75):
            y = height * ratio
            painter.drawLine(QPointF(0, y), QPointF(width, y))

        self._paint_series(painter, self.trace.mic, theme.DARK.accent, width, height)
        self._paint_series(
            painter, self.trace.speaker, theme.style_for("说话").core, width, height
        )

    def _paint_series(
        self, painter: QPainter, values: tuple[float, ...], color: str, width: int, height: int
    ) -> None:
        if len(values) < 2 or width <= 1:
            return
        # 最新的一点贴右边缘：数据从右边滚进来，符合「刚发生的事在右边」
        offset = self.trace.points - len(values)
        step = width / max(1, self.trace.points - 1)

        polygon = QPolygonF()
        for index, value in enumerate(values):
            x = (offset + index) * step
            y = height - 2 - value * (height - 6)
            polygon.append(QPointF(x, y))

        pen = QPen(QColor(color), 1.6)
        painter.setPen(pen)
        painter.drawPolyline(polygon)


class TechPanel(QWidget):
    """四个分区：电平波形 / 设备 / 参数 / 事件日志。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.trace = LevelTrace()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.GAP)

        layout.addWidget(self._build_waveform())
        self._device_grid, self._device_box = self._build_grid("设备")
        self._param_grid, self._param_box = self._build_grid("参数")
        layout.addWidget(self._device_box)
        layout.addWidget(self._param_box)
        layout.addWidget(self._build_event_log())

    # ---------- 装配 ----------

    def _build_waveform(self) -> QWidget:
        box = QFrame()
        box.setObjectName("Panel")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        layout.setSpacing(6)

        caption = QLabel("电平波形（🎤 麦克风 · 🔊 播放）")
        caption.setObjectName("Dim")
        layout.addWidget(caption)

        self.waveform = WaveformWidget(self.trace)
        layout.addWidget(self.waveform)
        return box

    def _build_grid(self, title: str) -> tuple[QGridLayout, QWidget]:
        box = QFrame()
        box.setObjectName("Panel")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        outer.setSpacing(6)

        caption = QLabel(title)
        caption.setObjectName("Dim")
        outer.addWidget(caption)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)
        return grid, box

    def _build_event_log(self) -> QWidget:
        box = QFrame()
        box.setObjectName("Panel")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        layout.setSpacing(6)

        caption = QLabel(f"事件日志（最近 {EVENT_LOG_LIMIT} 条）")
        caption.setObjectName("Dim")
        layout.addWidget(caption)

        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumBlockCount(EVENT_LOG_LIMIT)
        self.event_log.setFixedHeight(150)
        layout.addWidget(self.event_log)
        return box

    # ---------- 更新 ----------

    def push_level(self, level: float, source: str) -> None:
        self.trace.push(level, source)
        self.waveform.refresh()

    def set_environment(self, sections: list[tuple[str, list[tuple[str, str]]]]) -> None:
        """刷新设备与参数两张表。

        重建整张表而不是逐行改：这个面板只在打开时刷一次，
        而「哪一行对应哪个键」一旦错位就会显示成别人的值 —— 那比空着更糟。
        """
        mapping = {"设备": self._device_grid, "参数": self._param_grid}
        for title, rows in sections:
            grid = mapping.get(title)
            if grid is None:
                continue
            _clear_grid(grid)
            for index, (label, value) in enumerate(rows):
                key = QLabel(label)
                key.setObjectName("Dim")
                val = QLabel(value)
                val.setWordWrap(True)
                val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                grid.addWidget(key, index, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(val, index, 1)

    def log_event(self, event: str, payload: dict) -> None:
        """原样打一行事件。

        不解析、不美化 —— 排障时最有用的是**原始载荷**，
        而不是别人替你总结过的版本。
        """
        summary = ", ".join(f"{k}={v!r}" for k, v in payload.items())
        self.event_log.appendPlainText(f"{event:<16} {summary}")

    def clear_log(self) -> None:
        self.event_log.clear()


def _clear_grid(grid: QGridLayout) -> None:
    while grid.count():
        item = grid.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
