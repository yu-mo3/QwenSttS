"""pytest 全局配置。

这里做的两件事都**必须在任何 Qt import 之前**完成，所以只能放在 conftest：

1. `QT_QPA_PLATFORM=offscreen` —— 没有显示器的环境（CI、SSH、后台任务）
   也能建 Qt 控件
2. `ensure_qt_runtime()` —— conda 的旧 ICU 会抢走 Qt6Core 需要的 `ucnv_*`
   符号，导致 `import PySide6.QtCore` 直接失败。见 app/ui/qt_runtime.py

两件事都被 try 包住：PySide6 是 M4 才引入的依赖，
没装它的时候**其余 225 条纯逻辑测试必须照常能跑**。
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:  # pragma: no cover - 取决于环境是否装了 PySide6
    from app.ui.qt_runtime import ensure_qt_runtime

    ensure_qt_runtime()
except Exception:  # noqa: BLE001 - 修不了就不修，让真正需要 Qt 的测试自己 skip
    pass
