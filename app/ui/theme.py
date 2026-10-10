"""L6 表现层的视觉常量与状态配色。

**这个模块刻意不 import Qt** —— 配色与状态映射是纯数据 + 纯函数，
因此可以被无显示器的单元测试锁住。项目里已有的测试全都是「纯函数测试」
（文件名带 _pure），这一层没有理由破例。

Qt 侧的转换（hex -> QColor）放在用到的地方，不放在这里。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.orchestrator.states import AssistantState

# ══════════════════════════════════════════════════════
#  尺寸与字体
# ══════════════════════════════════════════════════════

WINDOW_MIN_WIDTH = 880
WINDOW_MIN_HEIGHT = 560
WINDOW_DEFAULT_WIDTH = 1040
WINDOW_DEFAULT_HEIGHT = 680

# 光球区域（左栏）的固定宽度；右栏吸收剩余空间
ORB_COLUMN_WIDTH = 380
ORB_DIAMETER = 240

RADIUS = 12          # 统一圆角
GAP = 12             # 统一间距
PAD = 16             # 统一内边距

FONT_FAMILY = "Microsoft YaHei UI"
FONT_SIZE_BASE = 10
FONT_SIZE_SMALL = 9
FONT_SIZE_TITLE = 13

# 动画刷新率。30fps 对呼吸/脉动这类慢动作足够，
# 而 60fps 会让 8 核笔记本的风扇转起来 —— 一个语音助手不该这样。
ANIMATION_FPS = 30

# 状态切换时颜色与半径的过渡时长（毫秒）。
# 没有过渡的话，四态之间会像开关一样生硬跳变。
TRANSITION_MS = 250


# ══════════════════════════════════════════════════════
#  配色
# ══════════════════════════════════════════════════════

@dataclass(frozen=True)
class Palette:
    """一套界面配色。全部用 #RRGGBB 字符串，方便直接进样式表与测试。"""

    window: str
    panel: str
    panel_alt: str
    border: str
    text: str
    text_dim: str
    accent: str
    danger: str


DARK = Palette(
    window="#12151C",
    panel="#1A1F29",
    panel_alt="#222836",
    border="#2E3648",
    text="#E6EAF2",
    text_dim="#8A94A6",
    accent="#4ADE80",
    danger="#F87171",
)


@dataclass(frozen=True)
class StateStyle:
    """一个助手状态对应的视觉表现。"""

    core: str        # 球心颜色
    glow: str        # 外圈光晕颜色
    label: str       # 状态栏文字
    hint: str        # 状态栏下方的提示语（告诉用户现在该干什么）


#: 四态各自的配色。
#:
#: 选色理由：待机要冷、要不抢眼（她没在干活）；聆听要亮、要有生命力
#: （用户在说话，球应该在回应他）；思考要暖而收敛（在等云端）；
#: 说话要最饱和（这是唯一的「输出」态）。
STATE_STYLES: dict[AssistantState, StateStyle] = {
    AssistantState.IDLE: StateStyle(
        core="#3AA6B9",
        glow="#1E5F6B",
        label="待机",
        hint="喊一声「阿源」，或点下面的「点击说话」",
    ),
    AssistantState.LISTENING: StateStyle(
        core="#4ADE80",
        glow="#1F7A45",
        label="聆听中",
        hint="说吧，说完停顿一下我就知道你说完了",
    ),
    AssistantState.THINKING: StateStyle(
        core="#FBBF24",
        glow="#8A6410",
        label="思考中",
        hint="正在想怎么回你",
    ),
    AssistantState.SPEAKING: StateStyle(
        core="#C084FC",
        glow="#6B3FA0",
        label="说话中",
        hint="喊一声「停」我就会停下来",
    ),
    AssistantState.EXITING: StateStyle(
        core="#8A94A6",
        glow="#3A4150",
        label="正在退出",
        hint="正在收拾东西",
    ),
}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError(f"不是 #RRGGBB 颜色：{value!r}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix_hex(first: str, second: str, t: float) -> str:
    """两个 #RRGGBB 之间线性插值。t=0 取 first，t=1 取 second。

    放在 theme 而不是 orb：它是**配色运算**，不需要 Qt。
    QColor 的插值还牵扯整数舍入与 alpha 语义，而这里只要 RGB ——
    纯整数运算更容易写对，也更容易测。

    非法输入返回 first 而不是抛异常：这是每 33ms 调一次的绘制路径，
    为一个人手写错的颜色串让界面崩掉不值得。
    """
    t = min(max(t, 0.0), 1.0)
    try:
        ar, ag, ab = _hex_to_rgb(first)
        br, bg, bb = _hex_to_rgb(second)
    except ValueError:
        return first
    return "#%02X%02X%02X" % (
        round(ar + (br - ar) * t),
        round(ag + (bg - ag) * t),
        round(ab + (bb - ab) * t),
    )


def style_for(state: AssistantState | str) -> StateStyle:
    """按状态取配色。未知状态回落到待机，而不是抛异常。

    取舍很明确：界面因为一个拼错的状态名而崩掉，比显示成待机糟糕得多 ——
    用户在那种情况下连「阿源坏了」都说不清楚。
    """
    if isinstance(state, str):
        try:
            state = AssistantState(state)
        except ValueError:
            return STATE_STYLES[AssistantState.IDLE]
    return STATE_STYLES.get(state, STATE_STYLES[AssistantState.IDLE])


# ══════════════════════════════════════════════════════
#  样式表
# ══════════════════════════════════════════════════════

def stylesheet(palette: Palette = DARK) -> str:
    """整个应用的 QSS。集中在一处，避免颜色散落在各个控件里。"""
    return f"""
QWidget {{
    background: {palette.window};
    color: {palette.text};
    font-family: "{FONT_FAMILY}";
    font-size: {FONT_SIZE_BASE}pt;
}}
QFrame#Panel {{
    background: {palette.panel};
    border: 1px solid {palette.border};
    border-radius: {RADIUS}px;
}}
QLabel#Title {{ font-size: {FONT_SIZE_TITLE}pt; font-weight: 600; }}
QLabel#Dim {{ color: {palette.text_dim}; font-size: {FONT_SIZE_SMALL}pt; }}
QLabel#StateLabel {{ font-size: 15pt; font-weight: 600; }}
QPushButton {{
    background: {palette.panel_alt};
    border: 1px solid {palette.border};
    border-radius: {RADIUS}px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {palette.accent}; }}
QPushButton:disabled {{ color: {palette.text_dim}; border-color: {palette.border}; }}
QPushButton#Primary {{
    background: {palette.accent};
    color: #0B0E14;
    border: none;
    font-weight: 600;
}}
QPushButton#Danger:hover {{ border-color: {palette.danger}; }}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {palette.panel_alt};
    border: 1px solid {palette.border};
    border-radius: {RADIUS}px;
    padding: 6px 10px;
    selection-background-color: {palette.accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border-color: {palette.accent};
}}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {palette.border}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {palette.text_dim}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QToolButton {{
    background: transparent; border: none; padding: 4px 8px; color: {palette.text_dim};
}}
QToolButton:hover {{ color: {palette.text}; }}
QSplitter::handle {{ background: {palette.border}; }}
"""
