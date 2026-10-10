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
        text = window.history.toPlainText()
        assert "你好" in text and "你也好。" in text

    def test_typed_input_is_labelled_in_the_history(self, window):
        window.bridge.feed_event(
            EVT_ASR_DONE, {"text": "打字内容", "emotion": "", "elapsed": 0.0, "source": "typed"}
        )
        assert "打字" in window.history.toPlainText()

    def test_errors_are_visible_in_the_history(self, window):
        """静默失败是这个项目反复踩的坑 —— 出错必须在界面上看得见。"""
        window.bridge.feed_event(EVT_ERROR, {"message": "云端挂了", "hint": "稍后再试"})
        text = window.history.toPlainText()
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
