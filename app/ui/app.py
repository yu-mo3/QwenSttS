r"""阿源 · L6 表现层入口。

    .venv\Scripts\python.exe -m app.ui              启动图形界面
    .venv\Scripts\python.exe -m app.ui --no-session 只开界面，不启动语音会话（调界面用）

VSCode 里用「▶ 启动阿源（界面）」调试配置，见 .vscode/launch.json。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许两种启动方式：python -m app.ui / python app/ui/app.py
# （VSCode 的调试配置用的是后者）。直接跑脚本时 sys.path[0] 是 app/ui/，
# 找不到 app 包，所以这里补一次 —— 与 app/main.py 的做法一致。
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 必须在 PySide6.QtCore 之前 —— 它会在 Qt 之前修好 conda 环境下的 ICU 问题。
# 直接跑脚本时父包 __init__ 未必先执行，所以这里显式导入一次（幂等）。
from app.ui.qt_runtime import ensure_qt_runtime  # noqa: E402

ensure_qt_runtime()

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import Config  # noqa: E402
from app.core.logger import ensure_utf8_stdout, get_logger, setup_logging  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.controller import SessionController  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.tray import AyuanTray  # noqa: E402

log = get_logger(__name__)

#: 退出时最多等会话线程多久。到点就放弃 —— 用户点了关闭，窗口必须关掉。
SHUTDOWN_TIMEOUT = 3.0

#: 静默多少秒回待机。与 VoiceAssistant 的默认值保持一致。
DEFAULT_IDLE_TIMEOUT = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ayuan-ui",
        description="阿源 · 图形界面",
    )
    parser.add_argument(
        "--no-session",
        action="store_true",
        dest="no_session",
        help="只开界面，不启动语音会话（不需要麦克风，用于调界面）",
    )
    parser.add_argument(
        "--idle",
        type=float,
        default=None,
        help="静默多少秒回待机（默认取 .env 里的 AYUAN_IDLE_TIMEOUT，再默认 30）",
    )
    parser.add_argument(
        "--threshold", type=float, default=None, help="唤醒阈值（默认 0.2）"
    )
    parser.add_argument(
        "--stop-threshold",
        type=float,
        default=None,
        dest="stop_threshold",
        help="「停」的检测阈值（默认与唤醒阈值相同）",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    ensure_utf8_stdout()
    args = build_parser().parse_args(argv)

    config = Config.load()
    setup_logging("DEBUG" if args.verbose else config.log_level)
    config.ensure_dirs()

    # 只把程序名交给 Qt：其余参数是给我们自己的，
    # 交给 Qt 解析会被它当成未知选项直接退出进程
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("阿源")
    app.setApplicationDisplayName("阿源 · QwenSttS")
    app.setStyleSheet(theme.stylesheet())

    # 命令行 > .env > 代码默认值 —— 与 Config 的优先级保持一致
    controller = SessionController(
        config,
        idle_timeout=(
            args.idle if args.idle is not None else config.perception.idle_timeout
        ),
        wake_threshold=(
            args.threshold
            if args.threshold is not None
            else config.perception.wake_threshold
        ),
        stop_threshold=(
            args.stop_threshold
            if args.stop_threshold is not None
            else config.perception.effective_stop_threshold
        ),
    )
    window = MainWindow(config, controller)

    # 托盘要留一个强引用 —— 只被局部变量持有的 QObject 会被 GC 掉，
    # 表现是「图标闪一下就没了」，而且不会有任何报错
    tray = AyuanTray(window, parent=app)
    if tray.show():
        window.tray = tray
        # 有托盘时，关掉窗口不等于退出程序 —— 她还在待命
        app.setQuitOnLastWindowClosed(False)
    else:
        log.info("没有系统托盘，关窗即退出")

    window.show()

    if args.no_session:
        log.info("--no-session：只开界面，不启动语音会话")
    else:
        controller.start()

    def _shutdown() -> None:
        """关窗时把会话线程收干净，否则麦克风可能还被占着。"""
        controller.stop()
        if not controller.wait_stopped(SHUTDOWN_TIMEOUT):
            log.warning("会话线程未能在 %.1fs 内退出，直接退出进程", SHUTDOWN_TIMEOUT)

    app.aboutToQuit.connect(_shutdown)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
