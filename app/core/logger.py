"""统一日志配置：控制台 + 可选文件双输出。

用法：
    from app.core.logger import setup_logging, get_logger
    setup_logging()
    log = get_logger(__name__)
    log.info("就绪")
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s │ %(levelname)-7s │ %(name)-18s │ %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# 终端彩色（Windows 10+ 的 conhost / Windows Terminal 均支持 ANSI）
_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """给 levelname 上色，其余保持原样。"""

    def __init__(self, *, use_color: bool) -> None:
        super().__init__(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        if not self.use_color:
            return super().format(record)
        original = record.levelname
        color = _COLORS.get(original, "")
        record.levelname = f"{color}{original}{_RESET}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def ensure_utf8_stdout() -> None:
    """强制 stdout / stderr 使用 UTF-8。

    Windows 控制台默认是 GBK，中文与制表符会变成乱码。
    必须在「任何输出之前」调用 —— 包括 argparse 的 --version，
    它会在 setup_logging() 之前就打印并退出。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def setup_logging(
    level: str | int = logging.INFO,
    *,
    log_file: Path | None = None,
    quiet_third_party: bool = True,
) -> None:
    """初始化根 logger。重复调用是安全的（会先清空已有 handler）。"""
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    ensure_utf8_stdout()

    stream = sys.stdout
    console = logging.StreamHandler(stream)
    console.setFormatter(_ColorFormatter(use_color=_supports_color(stream)))
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
        root.addHandler(file_handler)

    if quiet_third_party:
        # httpx2 是 openai>=3.x 使用的 HTTP 客户端，日志名与 httpx 不同
        for noisy in ("urllib3", "httpx", "httpx2", "httpcore", "httpcore2", "openai", "asyncio"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取带命名空间的 logger。"""
    return logging.getLogger(name)
