"""对话历史面板。

## 分成模型与控件两层

HistoryModel 是**纯 Python**，不 import Qt：时间戳格式、条目标签、
超出上限时丢最旧的…… 这些规则都能被无显示器的测试逐条锁住。
HistoryPanel 只负责把条目渲染成气泡。

这么分是因为「历史里到底显示了什么」是这个面板唯一值得测的东西，
而气泡的圆角半径不值得测。

## 增量追加，不整体重绘

每收到一条就追加一个气泡，而不是清空重画整列。重画会让滚动位置跳动，
用户正在往回翻的时候尤其难受。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.ui import theme

#: 最多保留多少条。常驻一整天的助手不该把内存吃光，
#: 而排障看最近几十条就够了。
DEFAULT_LIMIT = 200

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_NOTE = "note"

#: 用户条目的来源标注。「打字」必须看得出来 —— 否则排障时会去怀疑麦克风。
_SOURCE_LABEL = {"speech": "你", "typed": "你 · 打字"}


@dataclass(frozen=True)
class HistoryEntry:
    """一条对话记录。纯数据。"""

    role: str
    text: str
    clock: str          # "09:41"
    meta: str = ""      # 附加说明，例如 "首字 0.46s"
    source: str = "speech"

    @property
    def label(self) -> str:
        if self.role == ROLE_USER:
            return _SOURCE_LABEL.get(self.source, _SOURCE_LABEL["speech"])
        if self.role == ROLE_ASSISTANT:
            return "阿源"
        return ""


def format_clock(timestamp: float) -> str:
    """把时间戳格式化成 时:分。"""
    return time.strftime("%H:%M", time.localtime(timestamp))


def describe_first_byte(first_byte: object) -> str:
    """把首字耗时写成一句人话。拿不到就不显示，而不是显示 0.00s。

    0.00s 会被读成「快得不可思议」，而真相是「没有测到」。
    """
    if isinstance(first_byte, bool) or not isinstance(first_byte, (int, float)):
        return ""
    if first_byte <= 0:
        return ""
    return f"首字 {float(first_byte):.2f}s"


class HistoryModel:
    """对话历史的数据层。**不依赖 Qt**，可直接单测。"""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self.limit = max(1, int(limit))
        self._entries: list[HistoryEntry] = []

    @property
    def entries(self) -> tuple[HistoryEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def _append(self, entry: HistoryEntry) -> HistoryEntry:
        self._entries.append(entry)
        # 丢最旧的：用户关心的是刚才说了什么
        if len(self._entries) > self.limit:
            del self._entries[: len(self._entries) - self.limit]
        return entry

    def add_user(
        self, text: str, *, source: str = "speech", now: float | None = None
    ) -> HistoryEntry:
        return self._append(
            HistoryEntry(
                role=ROLE_USER,
                text=text,
                clock=format_clock(now if now is not None else time.time()),
                source=source,
            )
        )

    def add_assistant(
        self, text: str, *, first_byte: object = None, now: float | None = None
    ) -> HistoryEntry:
        return self._append(
            HistoryEntry(
                role=ROLE_ASSISTANT,
                text=text,
                clock=format_clock(now if now is not None else time.time()),
                meta=describe_first_byte(first_byte),
            )
        )

    def add_note(self, text: str, *, now: float | None = None) -> HistoryEntry:
        """系统提示：被打断、重试、静默超时、出错。

        这些**必须**进历史，不能只写日志 —— 「静默失败」是这个项目
        反复踩的坑（M2 的合成全空、M3 的输入线程死亡），
        界面上看不见就等于没发生。
        """
        return self._append(
            HistoryEntry(
                role=ROLE_NOTE,
                text=text,
                clock=format_clock(now if now is not None else time.time()),
            )
        )


# ══════════════════════════════════════════════════════
#  控件
# ══════════════════════════════════════════════════════

#: 气泡最大宽度占面板宽度的比例。太宽会看不出「谁说的」。
_BUBBLE_WIDTH_RATIO = 0.82


class HistoryPanel(QWidget):
    """对话历史：可滚动的气泡列表。"""

    def __init__(self, parent: QWidget | None = None, *, limit: int = DEFAULT_LIMIT) -> None:
        super().__init__(parent)
        self.model = HistoryModel(limit=limit)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._scroll)

        self._container = QWidget()
        self._list = QVBoxLayout(self._container)
        self._list.setContentsMargins(2, 2, 8, 2)
        self._list.setSpacing(8)
        self._list.addStretch(1)
        self._scroll.setWidget(self._container)

        # 存 (外层行, 气泡框) —— 改宽度要改的是气泡框，删控件要删的是外层行
        self._bubbles: list[tuple[QWidget, QFrame]] = []
        self._placeholder = QLabel("还没有对话。喊一声「阿源」，或直接在下面打字。")
        self._placeholder.setObjectName("Dim")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._list.insertWidget(0, self._placeholder)

    # ---------- 追加 ----------

    def add_user(self, text: str, source: str = "speech") -> HistoryEntry:
        return self._add(self.model.add_user(text, source=source))

    def add_assistant(self, text: str, first_byte: object = None) -> HistoryEntry:
        return self._add(self.model.add_assistant(text, first_byte=first_byte))

    def add_note(self, text: str) -> HistoryEntry:
        return self._add(self.model.add_note(text))

    def clear(self) -> None:
        self.model.clear()
        for row, _bubble in self._bubbles:
            self._list.removeWidget(row)
            row.deleteLater()
        self._bubbles.clear()
        self._placeholder.setVisible(True)

    def _bubble_width(self) -> int:
        """气泡的最大宽度。跟着面板宽度走，窗口拉大时气泡也跟着变宽。

        用面板自己的宽度、而不是滚动区的 viewport：viewport 要等布局跑完
        才准，而 resize() 之后立刻就要一个正确的值（新增气泡时就要用）。
        """
        width = self.width()
        if width <= 0:
            width = self._scroll.viewport().width()
        if width <= 0:
            return 420          # 还没布局出来时的合理兜底
        return max(180, int(width * _BUBBLE_WIDTH_RATIO))

    def _apply_bubble_width(self) -> None:
        limit = self._bubble_width()
        for _row, bubble in self._bubbles:
            bubble.setMaximumWidth(limit)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt 的命名
        super().resizeEvent(event)
        self._apply_bubble_width()

    def _add(self, entry: HistoryEntry) -> HistoryEntry:
        self._placeholder.setVisible(False)
        row, bubble = self._make_bubble(entry)
        # 插在 stretch 之前，保证气泡始终从头往下排
        self._list.insertWidget(self._list.count() - 1, row)
        self._bubbles.append((row, bubble))

        # 模型丢了最旧的，控件也要跟着丢，否则内存会一直涨
        while len(self._bubbles) > self.model.limit:
            old_row, _old_bubble = self._bubbles.pop(0)
            self._list.removeWidget(old_row)
            old_row.deleteLater()

        self._scroll_to_bottom()
        return entry

    def _make_bubble(self, entry: HistoryEntry) -> tuple[QWidget, QFrame]:
        """造一个气泡，返回 (外层行, 气泡框)。

        外层行负责左右对齐（靠一个 stretch 把气泡推到一侧），
        气泡框负责外观。分两层是因为「靠哪边」和「长什么样」是两件事。
        """
        bubble = QFrame()
        bubble.setObjectName(
            {
                ROLE_USER: "BubbleUser",
                ROLE_ASSISTANT: "BubbleAssistant",
            }.get(entry.role, "BubbleNote")
        )
        bubble.setMaximumWidth(self._bubble_width())

        layout = QVBoxLayout(bubble)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        header_parts = [entry.label, entry.clock]
        if entry.meta:
            header_parts.append(entry.meta)
        header = QLabel("  ·  ".join(p for p in header_parts if p))
        header.setObjectName("Dim")
        header.setWordWrap(True)
        layout.addWidget(header)

        body = QLabel(entry.text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)

        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        if entry.role == ROLE_NOTE:
            row.addStretch(1)
            row.addWidget(bubble)
            row.addStretch(1)
        elif entry.role == ROLE_USER:
            row.addStretch(1)
            row.addWidget(bubble)
        else:
            row.addWidget(bubble)
            row.addStretch(1)
        return wrapper, bubble

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---------- 供测试与排障读取 ----------

    def to_plain_text(self) -> str:
        """把历史摊平成文本。测试与「复制对话」都用它。"""
        lines = []
        for entry in self.model.entries:
            prefix = f"{entry.label}：" if entry.label else ""
            suffix = f"（{entry.meta}）" if entry.meta else ""
            lines.append(f"{prefix}{entry.text}{suffix}")
        return "\n".join(lines)
