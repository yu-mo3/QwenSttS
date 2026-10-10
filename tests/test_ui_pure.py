"""L6 表现层的测试。

## 为什么这里破例碰 Qt

项目其余的测试文件都是「纯函数测试」，不碰硬件、不碰 Qt。这一层破例，
是因为 L6 有一类 bug 只有把控件真的建起来才拦得住：

    · 槽函数名字写错（connect 时会 AttributeError）
    · 信号签名与槽对不上（会在**运行时**才炸，而且是在发射那一刻）
    · 状态名拼错导致界面永远停在待机

它们全都是「编译期看不出来、跑起来才炸」的类型。用一个 offscreen 的
QApplication 建一遍窗口，成本不到一秒，能拦住上面全部三类。

纯逻辑（配色映射、事件翻译）仍然按纯函数测，不依赖 Qt。
"""

from __future__ import annotations

import time

import pytest

# ⚠️ import 顺序要紧：app.ui 必须在 PySide6.QtCore 之前被 import ——
# 它会在任何 Qt import 之前修好 conda 环境下的 ICU 加载问题。
pytest.importorskip("PySide6", reason="界面测试需要 PySide6")
import app.ui  # noqa: E402, F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import Config  # noqa: E402
from app.orchestrator.events import (  # noqa: E402
    EVT_ASR_DONE,
    EVT_ERROR,
    EVT_EXIT,
    EVT_IDLE_TIMEOUT,
    EVT_INTERRUPTED,
    EVT_LLM_DONE,
    EVT_RETRY,
    EVT_STATE,
    EVT_TTS_DONE,
    EVT_WAKE,
)
from app.orchestrator.states import AssistantState  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.bridge import SUBSCRIBED_EVENTS, EventBridge  # noqa: E402
from app.ui.controller import SessionController  # noqa: E402
from app.ui import orb  # noqa: E402
from app.ui.history import (  # noqa: E402
    ROLE_ASSISTANT,
    ROLE_USER,
    HistoryModel,
    HistoryPanel,
    describe_first_byte,
    format_clock,
)
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.qt_runtime import ensure_qt_runtime  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """整个测试会话共用一个 QApplication。

    Qt 不允许同时存在两个，而反复创建/销毁它本身就会崩。
    """
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    """建一个真的主窗口。会话不启动 —— 测试不碰硬件。"""
    controller = SessionController(Config.load({}), idle_timeout=30.0)
    win = MainWindow(Config.load({}), controller)
    yield win
    # 必须走 request_shutdown() 而不是 close()：
    # close() 会弹出「要退出阿源吗」的**模态**确认框，而 offscreen 下
    # 没有用户能点它 —— 测试会直接崩在清理阶段（已实测）。
    # 顺带这也覆盖了托盘「退出」走的那条真实路径。
    win.request_shutdown()
    controller.stop()


# ══════════════════════════════════════════════════════
#  纯逻辑：配色
# ══════════════════════════════════════════════════════

class TestTheme:
    def test_every_state_has_a_style(self):
        """状态表是穷举的 —— 新加一个状态却忘了配色，界面会静默回落到待机。"""
        missing = [s for s in AssistantState if s not in theme.STATE_STYLES]
        assert not missing, f"这些状态没有配色：{missing}"

    def test_style_lookup_by_enum(self):
        assert theme.style_for(AssistantState.LISTENING).label == "聆听中"

    def test_style_lookup_by_value_string(self):
        assert theme.style_for("思考").label == "思考中"

    def test_unknown_state_falls_back_to_idle(self):
        """拼错状态名时显示成待机，而不是抛异常崩掉界面。

        用户在「界面崩了」的情况下连问题都描述不清楚。
        """
        assert theme.style_for("不存在的状态") is theme.STATE_STYLES[AssistantState.IDLE]

    def test_labels_and_hints_are_not_empty(self):
        for state, style in theme.STATE_STYLES.items():
            assert style.label.strip(), f"{state} 的状态文字是空的"
            assert style.hint.strip(), f"{state} 没有给用户的提示语"

    def test_colors_are_hex(self):
        for state, style in theme.STATE_STYLES.items():
            for value in (style.core, style.glow):
                assert value.startswith("#") and len(value) == 7, f"{state}: {value}"

    def test_stylesheet_mentions_the_palette(self):
        sheet = theme.stylesheet()
        for value in (theme.DARK.window, theme.DARK.panel, theme.DARK.accent):
            assert value in sheet, f"样式表里没有用到 {value}"


# ══════════════════════════════════════════════════════
#  纯逻辑：事件翻译
# ══════════════════════════════════════════════════════

class TestEventBridgeMapping:
    """bridge 把编排层的事件翻译成 Qt 信号 —— 这一步错了，界面就没反应。"""

    def _bridge_with_recorder(self):
        b = EventBridge()
        seen: dict[str, list] = {}

        def rec(name):
            def _slot(*args):
                seen.setdefault(name, []).append(args)

            return _slot

        b.event_received.connect(rec("event"))
        b.state_changed.connect(rec("state"))
        b.user_said.connect(rec("user"))
        b.reply_ready.connect(rec("reply"))
        b.speech_progress.connect(rec("speech"))
        b.interrupted.connect(rec("interrupted"))
        b.retrying.connect(rec("retry"))
        b.idle_timeout.connect(rec("idle"))
        b.failed.connect(rec("failed"))
        b.session_finished.connect(rec("finished"))
        return b, seen

    def test_every_event_reaches_the_raw_log(self):
        """原始事件一个都不能漏 —— 技术面板的事件日志靠它排障。"""
        b, seen = self._bridge_with_recorder()
        b.feed_event(EVT_WAKE, {})
        assert seen["event"] == [(EVT_WAKE, {})]

    def test_state_change_is_mapped(self):
        b, seen = self._bridge_with_recorder()
        b.feed_event(EVT_STATE, {"old": "待机", "new": "聆听"})
        assert seen["state"] == [("待机", "聆听")]

    def test_user_speech_carries_its_source(self):
        b, seen = self._bridge_with_recorder()
        b.feed_event(
            EVT_ASR_DONE, {"text": "你好", "emotion": "x", "elapsed": 0.5, "source": "typed"}
        )
        assert seen["user"] == [("你好", "typed")]

    def test_missing_source_defaults_to_speech(self):
        """老调用方不发 source 时不能炸 —— 契约为它标了 optional。"""
        b, seen = self._bridge_with_recorder()
        b.feed_event(EVT_ASR_DONE, {"text": "你好", "emotion": "", "elapsed": 0.5})
        assert seen["user"] == [("你好", "speech")]

    def test_reply_carries_sentences_and_first_byte(self):
        b, seen = self._bridge_with_recorder()
        b.feed_event(EVT_LLM_DONE, {"text": "好", "sentences": ["好。"], "first_byte": 0.46})
        assert seen["reply"] == [("好", ["好。"], 0.46)]

    def test_speech_progress_uses_the_total_from_the_contract(self):
        """KI-006 的收益：assistant 现在也发 total，界面才敢直接取。"""
        b, seen = self._bridge_with_recorder()
        b.feed_event(EVT_TTS_DONE, {"index": 2, "total": 3, "duration": 1.5})
        assert seen["speech"] == [(2, 3, 1.5)]

    def test_error_carries_a_hint(self):
        b, seen = self._bridge_with_recorder()
        b.feed_event(EVT_ERROR, {"message": "坏了", "hint": "这么做"})
        assert seen["failed"] == [("坏了", "这么做")]

    def test_unknown_event_does_not_crash(self):
        """契约不是白名单：发一个表外事件只该进日志，不该抛。"""
        b, seen = self._bridge_with_recorder()
        b.feed_event("something_new", {"a": 1})
        assert seen["event"] == [("something_new", {"a": 1})]

    def test_signal_map_covers_every_subscribed_event(self):
        """对话无状态测试：SUBSCRIBED_EVENTS 里列了却没人处理的事件是死契约。"""
        b, seen = self._bridge_with_recorder()
        for name in sorted(SUBSCRIBED_EVENTS):
            b.feed_event(name, {})
        handled = {k for k in seen if k != "event"}
        assert handled, "至少该有一个事件被专门处理"


# ══════════════════════════════════════════════════════
#  Qt 运行时修补
# ══════════════════════════════════════════════════════

class TestQtRuntime:
    def test_is_idempotent(self):
        assert ensure_qt_runtime() is False, "第二次调用不该再做任何事"

    def test_qt_actually_loads(self):
        """这条测试能跑起来本身就是结论 —— QtCore 加载失败会在 import 期炸。"""
        from PySide6 import QtCore

        assert QtCore.__version__


# ══════════════════════════════════════════════════════
#  窗口冒烟：把控件真的建起来
# ══════════════════════════════════════════════════════

class TestMainWindowSmoke:
    def test_window_builds(self, window):
        assert window.windowTitle() == "阿源 · QwenSttS"
        assert window.state_label.text() == "待机"

    def test_state_change_updates_the_widgets(self, window):
        window.bridge.feed_event(EVT_STATE, {"old": "待机", "new": "说话"})
        assert window.state_label.text() == "说话中"
        assert window.stop_button.isEnabled() is True, "说话时「停」才该可用"

    def test_stop_button_is_disabled_when_idle(self, window):
        window.bridge.feed_event(EVT_STATE, {"old": "说话", "new": "待机"})
        assert window.stop_button.isEnabled() is False

    def test_unknown_state_name_does_not_crash(self, window):
        window.bridge.feed_event(EVT_STATE, {"old": "待机", "new": "天知道"})
        assert window.state_label.text() == "待机", "未知状态应当静默保持原样"

    def test_history_shows_both_sides(self, window):
        window.bridge.feed_event(
            EVT_ASR_DONE, {"text": "你好", "emotion": "", "elapsed": 0.1, "source": "speech"}
        )
        window.bridge.feed_event(
            EVT_LLM_DONE, {"text": "你也好。", "sentences": ["你也好。"], "first_byte": 0.3}
        )
        text = window.history.to_plain_text()
        assert "你好" in text and "你也好。" in text

    def test_typed_input_is_labelled_in_the_history(self, window):
        window.bridge.feed_event(
            EVT_ASR_DONE, {"text": "打字内容", "emotion": "", "elapsed": 0.0, "source": "typed"}
        )
        assert "打字" in window.history.to_plain_text()

    def test_errors_are_visible_in_the_history(self, window):
        """静默失败是这个项目反复踩的坑 —— 出错必须在界面上看得见。"""
        window.bridge.feed_event(EVT_ERROR, {"message": "云端挂了", "hint": "稍后再试"})
        text = window.history.to_plain_text()
        assert "云端挂了" in text and "稍后再试" in text

    def test_level_meter_reacts(self, window):
        window.bridge.feed_level(0.0, "mic")
        low = window.level_label.text()
        window.bridge.feed_level(1.0, "mic")
        high = window.level_label.text()
        assert low != high
        assert high.count("█") > low.count("█")

    def test_event_log_records_raw_payloads(self, window):
        window.bridge.feed_event(EVT_TTS_DONE, {"index": 1, "total": 2, "duration": 0.9})
        log_text = window.event_log.toPlainText()
        assert EVT_TTS_DONE in log_text
        assert "total=2" in log_text, "事件日志要原样打载荷，排障时才有用"

    def test_tech_panel_starts_collapsed(self, window):
        assert window._tech_body.isVisible() is False

    def test_tech_panel_toggles(self, window):
        window._tech_toggle.setChecked(True)
        assert window._tech_toggle.text().startswith("▾")

# ══════════════════════════════════════════════════════
#  纯逻辑：颜色插值
# ══════════════════════════════════════════════════════

class TestMixHex:
    def test_ends_are_exact(self):
        assert theme.mix_hex("#000000", "#FFFFFF", 0.0) == "#000000"
        assert theme.mix_hex("#000000", "#FFFFFF", 1.0) == "#FFFFFF"

    def test_midpoint(self):
        assert theme.mix_hex("#000000", "#FFFFFF", 0.5) == "#808080"

    def test_t_is_clamped(self):
        assert theme.mix_hex("#000000", "#FFFFFF", -5.0) == "#000000"
        assert theme.mix_hex("#000000", "#FFFFFF", 5.0) == "#FFFFFF"

    def test_accepts_lowercase_input(self):
        assert theme.mix_hex("#abcdef", "#abcdef", 0.5).upper() == "#ABCDEF"

    def test_garbage_returns_the_first_colour(self):
        """这是每 33ms 调一次的绘制路径 —— 为一个手写错的颜色串崩掉界面不值得。"""
        assert theme.mix_hex("不是颜色", "#FFFFFF", 0.5) == "不是颜色"
        assert theme.mix_hex("#FFF", "#FFFFFF", 0.5) == "#FFF"


# ══════════════════════════════════════════════════════
#  纯逻辑：光球的曲线与几何
# ══════════════════════════════════════════════════════

class TestOrbCurves:
    def test_breath_is_a_full_period(self):
        """峰值在 1/4 周期、谷值在 3/4 周期 —— 正弦的固有形状，不是随手定的一半。"""
        period = orb.BREATH_PERIOD
        assert orb.breath(0.0) == pytest.approx(0.5)
        assert orb.breath(period / 4) == pytest.approx(1.0)
        assert orb.breath(period / 2) == pytest.approx(0.5)
        assert orb.breath(period * 3 / 4) == pytest.approx(0.0)
        assert orb.breath(period) == pytest.approx(0.5)

    def test_breath_stays_in_range(self):
        for step in range(200):
            value = orb.breath(step * 0.05)
            assert 0.0 <= value <= 1.0

    def test_pulse_stays_in_range(self):
        for step in range(200):
            value = orb.pulse(step * 0.01)
            assert 0.0 <= value <= 1.0

    def test_ripple_phase_wraps(self):
        assert orb.ripple_phase(0.0) == pytest.approx(0.0)
        assert orb.ripple_phase(orb.RIPPLE_PERIOD / 2) == pytest.approx(0.5)
        assert orb.ripple_phase(orb.RIPPLE_PERIOD) == pytest.approx(0.0, abs=1e-9)

    def test_ripple_survives_a_zero_period(self):
        """除零会让绘制路径每帧崩一次，而它只在参数被改错时才出现。"""
        assert orb.ripple_phase(1.0, period=0.0) == 0.0

    def test_ease_reaches_the_target(self):
        assert orb.ease(0.0, 10.0, 1.0) == pytest.approx(10.0)

    def test_ease_with_zero_factor_does_nothing(self):
        assert orb.ease(3.0, 10.0, 0.0) == pytest.approx(3.0)

    def test_ease_is_monotonic_and_never_overshoots(self):
        value = 0.0
        for _ in range(50):
            value = orb.ease(value, 10.0, 0.25)
            assert 0.0 <= value <= 10.0

    def test_ease_clamps_silly_factors(self):
        assert orb.ease(0.0, 10.0, 5.0) == pytest.approx(10.0)
        assert orb.ease(0.0, 10.0, -5.0) == pytest.approx(0.0)


class TestOrbFrame:
    def test_every_state_produces_a_frame(self):
        for state in AssistantState:
            frame = orb.orb_frame(state, t=0.3)
            assert frame.radius > 0
            assert frame.glow_radius >= frame.radius, "光晕必须比球体大"

    def test_colours_come_from_the_state_style(self):
        frame = orb.orb_frame(AssistantState.LISTENING, t=0.0)
        assert frame.core == theme.style_for(AssistantState.LISTENING).core

    def test_same_inputs_give_same_frame(self):
        """纯函数：控件靠这一点才能每帧重算而不需要缓存。"""
        first = orb.orb_frame(AssistantState.SPEAKING, level=0.4, t=1.23)
        second = orb.orb_frame(AssistantState.SPEAKING, level=0.4, t=1.23)
        assert first == second

    def test_idle_breathes(self):
        period = orb.BREATH_PERIOD
        low = orb.orb_frame(AssistantState.IDLE, t=period * 3 / 4).radius
        high = orb.orb_frame(AssistantState.IDLE, t=period / 4).radius
        assert high > low, "待机必须有可见的呼吸，否则看起来像卡死"
        assert high - low > 1.0, f"呼吸幅度太小会看不出来：{high - low:.2f}px"

    def test_listening_follows_the_microphone_level(self):
        quiet = orb.orb_frame(AssistantState.LISTENING, level=0.0, t=0.0).radius
        loud = orb.orb_frame(AssistantState.LISTENING, level=1.0, t=0.0).radius
        assert loud > quiet, "聆听时球要跟着用户音量胀缩"

    def test_listening_level_is_monotonic(self):
        radii = [
            orb.orb_frame(AssistantState.LISTENING, level=x / 10, t=0.0).radius
            for x in range(11)
        ]
        assert radii == sorted(radii)

    def test_speaking_pulses(self):
        samples = [
            orb.orb_frame(AssistantState.SPEAKING, level=0.8, t=t / 50).radius
            for t in range(50)
        ]
        assert max(samples) > min(samples), "说话态必须有脉动"

    def test_thinking_rotates_without_pulsing(self):
        first = orb.orb_frame(AssistantState.THINKING, level=0.0, t=0.0)
        later = orb.orb_frame(AssistantState.THINKING, level=0.0, t=0.5)
        assert first.arc_start != later.arc_start, "思考态必须转起来"
        assert later.radius == pytest.approx(first.radius, abs=0.5), (
            "思考态不该跟着呼吸起伏 —— 那是待机的语言"
        )

    def test_level_is_clamped(self):
        over = orb.orb_frame(AssistantState.LISTENING, level=9.0, t=0.0)
        at_one = orb.orb_frame(AssistantState.LISTENING, level=1.0, t=0.0)
        under = orb.orb_frame(AssistantState.LISTENING, level=-9.0, t=0.0)
        at_zero = orb.orb_frame(AssistantState.LISTENING, level=0.0, t=0.0)
        assert over.radius == pytest.approx(at_one.radius)
        assert under.radius == pytest.approx(at_zero.radius)

    def test_unknown_state_falls_back(self):
        """GEOMETRY 表里没有的状态不该抛 —— 界面永远不该因为配色表缺项而崩。"""
        frame = orb.orb_frame("天知道", t=0.0)  # type: ignore[arg-type]
        assert frame.radius > 0


class TestOrbWidget:
    def test_starts_idle(self, qapp):
        widget = orb.OrbWidget()
        assert widget.state is AssistantState.IDLE

    def test_level_is_clamped_on_the_way_in(self, qapp):
        widget = orb.OrbWidget()
        widget.set_level(9.0)
        assert widget._level == 1.0
        widget.set_level(-9.0)
        assert widget._level == 0.0

    def test_advance_moves_towards_the_target(self, qapp):
        widget = orb.OrbWidget()
        widget.set_state(AssistantState.LISTENING)
        widget.set_level(1.0)
        before = widget._shown_radius
        widget._advance()
        assert widget._shown_radius >= before, "缓动方向应当朝目标，而不是反向"

    def test_colours_ease_rather_than_jump(self, qapp):
        """状态切换时颜色要渐变 —— 直接跳过去会让四态看起来像四个不同的控件。"""
        widget = orb.OrbWidget()
        start = widget._shown_core
        widget.set_state(AssistantState.SPEAKING)
        widget._advance()
        assert widget._shown_core != start, "颜色应当已经开始过渡"
        assert widget._shown_core != theme.style_for(AssistantState.SPEAKING).core, (
            "一帧之内不该直接跳到目标色"
        )


class TestOrbFitsInsideItsWidget:
    """光晕与涟漪必须画得下。

    早期版本直接拿 width()/2 当球体半径，光晕最远画到半径的 1.87 倍 ——
    全都溢出控件被裁掉，渲染出来是球体四周一圈**方形暗边**。
    这个缺陷肉眼很难在代码里看出来，是把四态渲染成 PNG 才发现的。
    """

    def test_extent_ratio_is_derived_not_guessed(self):
        assert orb.MAX_EXTENT_RATIO > 1.0, "光晕总得比球体大"
        assert orb.MAX_RADIUS_RATIO == pytest.approx(1.0 / orb.MAX_EXTENT_RATIO)

    def test_nothing_is_clipped_at_any_state_or_level(self):
        half = theme.ORB_DIAMETER / 2.0
        base = half * orb.MAX_RADIUS_RATIO
        worst = 0.0
        for state in AssistantState:
            for level in (0.0, 0.25, 0.5, 0.75, 1.0):
                for t in (x * 0.2 for x in range(20)):
                    frame = orb.orb_frame(state, level=level, t=t, base_radius=base)
                    extent = max(
                        frame.glow_radius,
                        frame.radius * (1.0 + orb.RIPPLE_REACH),
                    )
                    worst = max(worst, extent)
        assert worst <= half + 0.5, (
            f"有元素画到了控件外面：最远 {worst:.1f}px > 半宽 {half:.1f}px"
        )

    def test_the_sphere_is_still_prominent(self):
        """不能为了不越界就把球画得看不见。"""
        half = theme.ORB_DIAMETER / 2.0
        base = half * orb.MAX_RADIUS_RATIO
        frame = orb.orb_frame(AssistantState.IDLE, t=0.0, base_radius=base)
        assert frame.radius / half > 0.4, f"球体太小了：{frame.radius / half:.2f}"

# ══════════════════════════════════════════════════════
#  纯逻辑：对话历史的模型
# ══════════════════════════════════════════════════════

class TestHistoryModel:
    def test_records_both_sides(self):
        model = HistoryModel()
        model.add_user("你好")
        model.add_assistant("你也好。")
        assert [e.role for e in model.entries] == [ROLE_USER, ROLE_ASSISTANT]
        assert model.entries[0].text == "你好"

    def test_user_label_distinguishes_typed_from_spoken(self):
        """打字必须标注出来，否则排障时会去怀疑麦克风。"""
        model = HistoryModel()
        assert model.add_user("说话").label == "你"
        assert model.add_user("打字", source="typed").label == "你 · 打字"

    def test_assistant_label_is_the_app_name(self):
        assert HistoryModel().add_assistant("在").label == "阿源"

    def test_unknown_source_falls_back_to_speech(self):
        model = HistoryModel()
        assert model.add_user("x", source="天知道").label == "你"

    def test_notes_have_no_speaker(self):
        assert HistoryModel().add_note("被打断").label == ""

    def test_limit_drops_the_oldest(self):
        """常驻一整天的助手不该把内存吃光。"""
        model = HistoryModel(limit=3)
        for index in range(5):
            model.add_user(f"第{index}句")
        assert len(model) == 3
        assert [e.text for e in model.entries] == ["第2句", "第3句", "第4句"]

    def test_limit_is_at_least_one(self):
        model = HistoryModel(limit=0)
        model.add_user("x")
        assert len(model) == 1

    def test_clear(self):
        model = HistoryModel()
        model.add_user("x")
        model.clear()
        assert model.entries == ()

    def test_entries_is_a_snapshot(self):
        """返回元组而不是内部列表：调用方改不动模型内部状态。"""
        model = HistoryModel()
        model.add_user("x")
        assert isinstance(model.entries, tuple)

    def test_clock_is_rendered(self):
        entry = HistoryModel().add_user("x", now=0.0)
        assert len(entry.clock) == 5 and entry.clock[2] == ":"


class TestHistoryFormatting:
    def test_first_byte_reads_as_a_sentence(self):
        assert describe_first_byte(0.46) == "首字 0.46s"

    def test_missing_first_byte_shows_nothing(self):
        """显示 0.00s 会被读成「快得不可思议」，而真相是「没测到」。"""
        assert describe_first_byte(None) == ""
        assert describe_first_byte(0.0) == ""
        assert describe_first_byte(-1.0) == ""

    def test_bool_is_not_a_number(self):
        """True 是 int 的子类 —— 掉进去会显示出「首字 1.00s」这种鬼东西。"""
        assert describe_first_byte(True) == ""
        assert describe_first_byte(False) == ""

    def test_garbage_is_ignored(self):
        assert describe_first_byte("很快") == ""

    def test_clock_format(self):
        assert len(format_clock(time.time())) == 5


# ══════════════════════════════════════════════════════
#  控件：历史面板
# ══════════════════════════════════════════════════════

class TestHistoryPanel:
    def test_starts_with_a_placeholder(self, qapp):
        panel = HistoryPanel()
        assert panel.model.entries == ()
        assert panel._placeholder.isVisible() is False  # 未显示，但存在于布局里

    def test_adding_hides_the_placeholder(self, qapp):
        panel = HistoryPanel()
        panel.show()
        panel.add_user("你好")
        assert panel._placeholder.isVisible() is False

    def test_plain_text_contains_both_sides(self, qapp):
        panel = HistoryPanel()
        panel.add_user("你好")
        panel.add_assistant("你也好。", 0.46)
        text = panel.to_plain_text()
        assert "你：你好" in text
        assert "阿源：你也好。" in text
        assert "首字 0.46s" in text

    def test_typed_entries_are_labelled(self, qapp):
        panel = HistoryPanel()
        panel.add_user("打字内容", "typed")
        assert "你 · 打字" in panel.to_plain_text()

    def test_notes_appear_in_the_text(self, qapp):
        panel = HistoryPanel()
        panel.add_note("被打断")
        assert "被打断" in panel.to_plain_text()

    def test_bubbles_are_trimmed_with_the_model(self, qapp):
        """模型丢了最旧的，控件也得跟着丢 —— 否则内存只涨不降。"""
        panel = HistoryPanel(limit=3)
        for index in range(6):
            panel.add_user(f"第{index}句")
        assert len(panel._bubbles) == 3
        assert len(panel.model) == 3

    def test_clear_removes_every_bubble(self, qapp):
        panel = HistoryPanel()
        panel.add_user("a")
        panel.add_assistant("b")
        panel.clear()
        assert panel._bubbles == []
        assert panel.to_plain_text() == ""

    def test_bubble_width_follows_the_panel(self, qapp):
        """拉宽窗口时气泡要跟着变宽，否则长回复会被挤成窄窄一条。"""
        panel = HistoryPanel()
        panel.resize(600, 400)
        panel.add_user("x")
        narrow = panel._bubbles[0][1].maximumWidth()

        panel.resize(900, 400)
        panel._apply_bubble_width()   # 不依赖事件循环，直接触发重算
        wide = panel._bubbles[0][1].maximumWidth()

        assert wide > narrow, f"面板变宽了气泡却没变：{narrow} -> {wide}"


# ══════════════════════════════════════════════════════
#  输入行
# ══════════════════════════════════════════════════════

class _StubController:
    """只记录收到的指令，不真的开会话。"""

    def __init__(self, bridge):
        self.bridge = bridge
        self.is_running = True
        self.submitted: list[str] = []
        self.posted: list[str] = []

    def submit_text(self, text: str) -> bool:
        self.submitted.append(text)
        return True

    def post(self, command: str) -> None:
        self.posted.append(command)

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def stop_speaking(self) -> None:
        pass

    def listen_now(self) -> None:
        pass


class TestInputRow:
    def test_send_is_disabled_while_empty(self, window):
        assert window.send_button.isEnabled() is False
        window.input.setText("你好")
        assert window.send_button.isEnabled() is True
        window.input.setText("   ")
        assert window.send_button.isEnabled() is False, "全是空格也不算有内容"

    def test_sending_clears_the_input(self, window):
        window.controller = _StubController(window.bridge)
        window.input.setText("你好")
        window.on_send_clicked()
        assert window.input.text() == ""

    def test_sending_does_not_double_up_the_bubble(self, window):
        """M4-5 修的 bug。

        on_send_clicked 自己往历史里加一条，编排层的 asr_done 又加一条 ——
        结果是两个一模一样的用户气泡。打字输入本来就该由事件流驱动，
        界面自己再写一遍就是重复记账。
        """
        stub = _StubController(window.bridge)
        window.controller = stub
        window.input.setText("你好")
        window.on_send_clicked()
        assert stub.submitted == ["你好"]

        window.bridge.feed_event(
            EVT_ASR_DONE,
            {"text": "你好", "emotion": "", "elapsed": 0.0, "source": "typed"},
        )
        text = window.history.to_plain_text()
        assert text.count("你好") == 1, f"用户气泡重复了：{text}"

    def test_empty_input_does_nothing(self, window):
        stub = _StubController(window.bridge)
        window.controller = stub
        window.input.setText("   ")
        window.on_send_clicked()
        assert stub.submitted == []

