"""脸谱式光球：阿源的脸。

## 为什么是纯代码自绘

项目里没有任何美术素材，而四态之间需要**连续插值**（颜色、半径都要平滑
过渡）。切图片会有跳变，也绑定死了分辨率与配色。QPainter 画出来的东西
跟着状态连续变化，也不需要额外资源。

## 四态的动作语言

    待机   缓慢呼吸（周期约 4 秒）—— 她活着，但没在干活
    聆听   外扩涟漪 + 球体跟随**真实麦克风电平**胀缩 —— 用户在说，球在回应
    思考   一段弧沿圆周匀速旋转 —— 在等云端，有进展感但不催
    说话   脉动，振幅跟随**真实播放电平** —— 她真的在出声

## 测试策略

几何与曲线全部抽成纯函数（breath / pulse / ripple_phase / ease / orb_frame），
它们不碰 Qt，可以被无显示器的单元测试锁住。控件本身只负责把 OrbFrame
画出来 —— 这一层不测像素，测的是「给什么状态算出什么目标」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QConicalGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget

from app.orchestrator.states import AssistantState
from app.ui import theme

# ══════════════════════════════════════════════════════
#  曲线（纯函数）
# ══════════════════════════════════════════════════════

#: 待机的呼吸周期（秒）。太慢像死机，太快像在喘。
BREATH_PERIOD = 4.0

#: 说话的脉动频率（Hz）。接近正常语速的音节节奏。
SPEAK_PULSE_HZ = 2.2

#: 聆听态涟漪的扩散周期（秒）
RIPPLE_PERIOD = 1.8

#: 思考态旋转弧每秒转多少度
THINK_DEGREES_PER_SECOND = 150.0


def breath(t: float) -> float:
    """0~1 的呼吸曲线，周期 BREATH_PERIOD 秒。"""
    return 0.5 + 0.5 * math.sin(t * math.tau / BREATH_PERIOD)


def pulse(t: float, *, hz: float = SPEAK_PULSE_HZ) -> float:
    """0~1 的脉动曲线。"""
    return 0.5 + 0.5 * math.sin(t * math.tau * hz)


def ripple_phase(t: float, *, period: float = RIPPLE_PERIOD) -> float:
    """0~1 的涟漪进度（0 刚出生，1 已经散尽）。"""
    if period <= 0:
        return 0.0
    return (t % period) / period


def ease(current: float, target: float, factor: float) -> float:
    """把 current 往 target 推 factor 比例。factor 夹在 0~1。

    用它而不是线性插值，是因为状态切换要「快速起步、缓缓收尾」；
    而每帧推一个固定比例天然就是指数逼近，不会过冲。
    """
    factor = min(max(factor, 0.0), 1.0)
    return current + (target - current) * factor


# ══════════════════════════════════════════════════════
#  一帧的视觉结果
# ══════════════════════════════════════════════════════

@dataclass(frozen=True)
class OrbFrame:
    """光球在某一瞬间的几何。纯数据，没有 Qt 类型。"""

    core: str          # 球心颜色 #RRGGBB
    glow: str          # 外圈光晕颜色
    radius: float      # 球体半径（像素）
    glow_radius: float  # 光晕外沿半径（像素）
    ripple: float      # 涟漪进度 0~1（仅聆听态用得上）
    arc_start: float   # 旋转弧的起始角（度，仅思考态用得上）
    intensity: float   # 整体亮度 0~1，用于透明度

    def __post_init__(self) -> None:
        assert self.radius > 0, "半径必须为正"


#: 涟漪最外圈相对球体半径的外扩比例。三圈依次错开相位往外散。
RIPPLE_REACH = 0.75

#: 各状态的几何系数：
#:     (基础半径倍率, 电平对半径的增益, 光晕倍率, 电平对光晕的增益)
#: 抽成表而不是散在 if 分支里，是为了让「四态各自长什么样」有唯一答案，
#: 也为了让球体尺寸能**从这张表推导出来**（见 MAX_RADIUS_RATIO）。
GEOMETRY: dict[AssistantState, tuple[float, float, float, float]] = {
    AssistantState.IDLE: (0.94, 0.06, 1.16, 0.00),
    AssistantState.LISTENING: (0.98, 0.30, 1.18, 0.34),
    AssistantState.THINKING: (0.96, 0.04, 1.15, 0.00),
    AssistantState.SPEAKING: (0.98, 0.22, 1.20, 0.30),
    AssistantState.EXITING: (0.90, 0.02, 1.10, 0.00),
}


def orb_frame(
    state: AssistantState,
    *,
    level: float = 0.0,
    t: float = 0.0,
    base_radius: float = theme.ORB_DIAMETER / 2.0,
) -> OrbFrame:
    """算出某个状态在某一瞬间该长什么样。

    纯函数：给同样的 (state, level, t) 永远得到同样的结果。控件只负责画，
    因此「四态各自的动作」可以被无 GUI 的测试逐条断言。
    """
    level = min(max(level, 0.0), 1.0)
    radius_scale, level_gain, glow_scale, glow_gain = GEOMETRY.get(
        state, GEOMETRY[AssistantState.IDLE]
    )
    style = theme.style_for(state)

    ripple = 0.0
    arc_start = 0.0
    intensity = 0.6

    if state is AssistantState.IDLE:
        # 呼吸：半径与亮度一起起伏，像在轻轻换气
        b = breath(t)
        radius_scale += 0.05 * b
        glow_scale += 0.05 * b
        intensity = 0.35 + 0.20 * b

    elif state is AssistantState.LISTENING:
        # 聆听：球体跟着用户音量胀缩，涟漪一圈圈往外散。
        # 注意是**真实电平**，不是正弦假动作 —— 用户在说话，球在回应他。
        radius_scale += level_gain * level
        glow_scale += glow_gain * level
        ripple = ripple_phase(t)
        intensity = 0.55 + 0.45 * level

    elif state is AssistantState.THINKING:
        # 思考：一段弧匀速转，半径几乎不动 —— 有进展感，但不假装在说话
        arc_start = (t * THINK_DEGREES_PER_SECOND) % 360.0
        radius_scale += level_gain * level
        intensity = 0.5 + 0.2 * breath(t * 2.0)

    elif state is AssistantState.SPEAKING:
        # 说话：脉动振幅跟随真实播放电平
        p = pulse(t)
        radius_scale += level_gain * (0.35 + 0.65 * p) * (0.35 + 0.65 * level)
        glow_scale += glow_gain * level
        intensity = 0.6 + 0.4 * level

    radius = base_radius * radius_scale
    return OrbFrame(
        core=style.core,
        glow=style.glow,
        radius=radius,
        glow_radius=radius * glow_scale,
        ripple=ripple,
        arc_start=arc_start,
        intensity=min(max(intensity, 0.0), 1.0),
    )


def _worst_extent_ratio() -> float:
    """所有状态下「画出来的东西最远能到球体半径的几倍」。

    把 GEOMETRY 与曲线在参数网格上跑一遍取最大值 —— 这样它是**推导出来的**，
    而不是某个人手填的常数。以后调动作幅度时，球体大小会自动跟着让位。
    """
    worst = 0.0
    for state in GEOMETRY:
        for level in (0.0, 0.5, 1.0):
            for t in (x * 0.25 for x in range(24)):
                frame = orb_frame(state, level=level, t=t, base_radius=1.0)
                worst = max(
                    worst,
                    frame.glow_radius,
                    frame.radius * (1.0 + RIPPLE_REACH),
                )
    return worst


#: 最远绘制距离相对球体半径的倍率。
MAX_EXTENT_RATIO = _worst_extent_ratio()

#: 球体半径最多占控件半宽的比例。
#:
#: 这个值不是随手定的：早期版本直接拿 width()/2 当球体半径，结果光晕
#: 与涟漪全都画到了控件外面被裁掉 —— 表现是球体四周出现一个**方形的暗边**
#: （渲染成 PNG 后一眼就能看见）。现在球体大小由 MAX_EXTENT_RATIO 反推，
#: 任何幅度的调整都不会再把它撑出边界。
MAX_RADIUS_RATIO = 1.0 / MAX_EXTENT_RATIO


# ══════════════════════════════════════════════════════
#  控件
# ══════════════════════════════════════════════════════

#: 每帧把显示值往目标值推的比例。30fps 下约 0.25 对应 ~250ms 的过渡，
#: 与 theme.TRANSITION_MS 是一个量级。
_EASE_FACTOR = 0.25

#: 涟漪画几圈。三圈足够看出「一波接一波」，再多只是徒增绘制量。
_RIPPLE_COUNT = 3


class OrbWidget(QWidget):
    """画那颗球。**不含任何业务逻辑** —— 状态与电平都由外部喂进来。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        size = theme.ORB_DIAMETER
        self.setFixedSize(size, size)

        self._state = AssistantState.IDLE
        self._level = 0.0
        self._t = 0.0

        # 显示值（会朝目标缓动），初值取待机的静止形态，避免启动时从零胀开
        seed = orb_frame(AssistantState.IDLE, t=0.0, base_radius=self._base_radius())
        self._shown_radius = seed.radius
        self._shown_glow_radius = seed.glow_radius
        self._shown_intensity = seed.intensity
        self._shown_core = seed.core
        self._shown_glow = seed.glow
        self._shown_ripple = 0.0
        self._shown_arc = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, 1000 // theme.ANIMATION_FPS))
        self._timer.timeout.connect(self._advance)

    # ---------- 外部喂数据 ----------

    def set_state(self, state: AssistantState) -> None:
        self._state = state

    def set_level(self, level: float) -> None:
        """0~1 的电平。麦克风态与播放态共用这一个入口。"""
        self._level = min(max(float(level), 0.0), 1.0)

    @property
    def state(self) -> AssistantState:
        return self._state

    # ---------- 生命周期 ----------

    def showEvent(self, event) -> None:  # noqa: N802 — Qt 的命名
        super().showEvent(event)
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        # 窗口藏起来还按 30fps 重绘纯属浪费电
        self._timer.stop()
        super().hideEvent(event)

    # ---------- 每帧 ----------

    def _advance(self) -> None:
        dt = self._timer.interval() / 1000.0
        self._t += dt

        target = orb_frame(
            self._state,
            level=self._level,
            t=self._t,
            base_radius=self._base_radius(),
        )

        self._shown_radius = ease(self._shown_radius, target.radius, _EASE_FACTOR)
        self._shown_glow_radius = ease(
            self._shown_glow_radius, target.glow_radius, _EASE_FACTOR
        )
        self._shown_intensity = ease(
            self._shown_intensity, target.intensity, _EASE_FACTOR
        )
        # 颜色也缓动：状态切换时颜色渐变，而不是「啪」地跳过去
        self._shown_core = theme.mix_hex(self._shown_core, target.core, _EASE_FACTOR)
        self._shown_glow = theme.mix_hex(self._shown_glow, target.glow, _EASE_FACTOR)
        # 涟漪与旋转角**不做**缓动：它们是周期量，缓动会让它们永远追不上
        self._shown_ripple = target.ripple
        self._shown_arc = target.arc_start

        self.update()

    def _base_radius(self) -> float:
        """球体的基准半径。

        由 MAX_RADIUS_RATIO 收窄过 —— 直接把 width()/2 当半径会让光晕与
        涟漪溢出控件被裁掉，形成一圈方形暗边。
        """
        return (self.width() / 2.0) * MAX_RADIUS_RATIO

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        center = QPointF(self.width() / 2.0, self.height() / 2.0)

        if self._state is AssistantState.LISTENING:
            self._paint_ripples(painter, center)
        if self._state is AssistantState.THINKING:
            self._paint_arc(painter, center)

        self._paint_glow(painter, center, self._shown_glow)
        self._paint_core(painter, center, self._shown_core)

    def _paint_glow(self, painter: QPainter, center: QPointF, color_hex: str) -> None:
        outer = self._shown_glow_radius
        if outer <= 0:
            return
        color = QColor(color_hex)
        color.setAlphaF(min(0.55 * self._shown_intensity + 0.15, 1.0))

        gradient = QRadialGradient(center, outer)
        gradient.setColorAt(0.0, color)
        gradient.setColorAt(max(0.0, self._shown_radius / outer - 0.05), color)
        transparent = QColor(color)
        transparent.setAlphaF(0.0)
        gradient.setColorAt(1.0, transparent)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, outer, outer)

    def _paint_core(self, painter: QPainter, center: QPointF, color_hex: str) -> None:
        radius = self._shown_radius
        if radius <= 0:
            return
        core = QColor(color_hex)
        # 球心比主色亮一点，看起来才像「发光」而不是「一块色板」
        highlight = core.lighter(135)

        gradient = QRadialGradient(
            QPointF(center.x() - radius * 0.25, center.y() - radius * 0.3),
            radius * 1.4,
        )
        gradient.setColorAt(0.0, highlight)
        gradient.setColorAt(0.65, core)
        edge = QColor(core).darker(140)
        gradient.setColorAt(1.0, edge)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(center, radius, radius)

    def _paint_ripples(self, painter: QPainter, center: QPointF) -> None:
        """聆听态的外扩涟漪 —— 让它看起来在「接住」用户的声音。"""
        color = QColor(self._shown_core)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index in range(_RIPPLE_COUNT):
            # 三圈错开相位，才像一波接一波，而不是同心圆一起跳
            phase = (self._shown_ripple + index / _RIPPLE_COUNT) % 1.0
            radius = self._shown_radius * (1.0 + phase * RIPPLE_REACH)
            ring = QColor(color)
            ring.setAlphaF(max(0.0, 0.45 * (1.0 - phase)) * self._shown_intensity)
            pen = QPen(ring)
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.drawEllipse(center, radius, radius)

    def _paint_arc(self, painter: QPainter, center: QPointF) -> None:
        """思考态的旋转弧 —— 给出「正在处理」的进展感。"""
        radius = self._shown_glow_radius * 0.92
        if radius <= 0:
            return
        rect = QRectF(
            center.x() - radius, center.y() - radius, radius * 2, radius * 2
        )
        gradient = QConicalGradient(center, -self._shown_arc)
        head = QColor(self._shown_core)
        head.setAlphaF(min(1.0, self._shown_intensity + 0.2))
        tail = QColor(self._shown_core)
        tail.setAlphaF(0.0)
        gradient.setColorAt(0.0, head)
        gradient.setColorAt(0.28, tail)
        # 剩下 72% 完全透明 —— 只留一段弧，而不是整个圆环
        gradient.setColorAt(0.29, tail)
        gradient.setColorAt(1.0, tail)

        pen = QPen(gradient, 4.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
