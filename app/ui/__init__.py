"""L6 表现层：PySide6 桌面界面。

**铁律**：界面只发命令、只订阅事件，不认识任何模型。

这条铁律靠一个边界来守：本包里**只有 controller.py 允许 import 编排层**，
其余模块（theme / main_window / orb / history / tech_panel / settings_panel /
tray）只认 Qt 与自己的信号。

## 为什么这里有一行副作用

```
ensure_qt_runtime()
```

它必须在 **任何 PySide6.QtCore 被 import 之前** 执行，而 Python 保证
import 子模块时先执行父包的 __init__ —— 所以这里是唯一能保证顺序的地方。
它做什么、为什么需要，见 qt_runtime.py 的模块文档（一句话：conda 的旧 ICU
会抢走 Qt6Core 需要的 ucnv_* 符号，导致 QtCore 整个加载失败）。

在不需要它的机器上，这个函数只做一次目录检查就返回，没有副作用。
"""

from __future__ import annotations

from app.ui.qt_runtime import ensure_qt_runtime

# ⚠️ 必须早于任何 PySide6.QtCore 的 import。本行以下才允许 import Qt。
ensure_qt_runtime()

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """延迟 import：不装 PySide6 时也能 import 本包（跑纯逻辑测试）。"""
    from app.ui.app import main as _main

    return _main(argv)
