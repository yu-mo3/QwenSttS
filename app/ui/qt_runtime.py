r"""修一个 PySide6 在 conda 环境下的加载失败。

## 现象

    ImportError: DLL load failed while importing QtCore: 找不到指定的程序。

一装上 PySide6 6.12 就撞上，而且**只在 Anaconda 系的解释器上复现**。

## 根因（实测，有证据）

PySide6 6.12 的 Windows wheel **不再自带 ICU**（170 个 Qt DLL 里一个 icu 都没有），
于是 Qt6Core.dll 直接 import 系统里的 `icuuc.dll`。而 conda 会把
`D:\Anaconda\Library\bin` 塞进 DLL 搜索路径，那里的 ICU 只导出**带版本后缀**
的符号：

    D:\Anaconda\Library\bin\icuuc.dll    <- 只有 ucnv_open_73
    C:\Windows\System32\icuuc.dll         <- 有无后缀的 ucnv_open   ← Qt 要的是这个

用 PE 导入表逐个符号比对，Qt6Core 一共缺 **20 个**符号，全是 `ucnv_*`
（编码转换器）：`ucnv_open` / `ucnv_close` / `ucnv_toUnicode` / …
唯一的例外是一个前缀相同、但入口点不同的 ICU，于是整个 QtCore 加载失败。

## 办法

**在 Qt 之前把 System32 的 ICU 拉进进程。** Windows loader 按模块基名复用
已加载的 DLL，所以之后 Qt6Core import `icuuc.dll` 时拿到的就是这一份。

为什么不用「把 conda 的目录从搜索路径里摘掉」：那个路径是 conda 的
`pywin32.pth` 在解释器启动时加的，摘它要改别人的安装。
这里只在进程内预加载一次，不改环境、不改注册表、可重入。

为什么不用降级 PySide6：也能绕开，但那是把一个环境问题藏进版本号里，
下一个人升级时还会再撞一次。

## 什么时候**不**该动手

如果 PySide6 自带 ICU（老版本会），那 System32 那份反而可能更旧，
抢先加载会把好的那份挡掉 —— 帮了倒忙。所以这里先检查 wheel 里有没有
自带的 ICU，没有才插手。这个判断在「本来就正常」的机器上会直接返回，
不留任何副作用。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from app.core.logger import get_logger

log = get_logger(__name__)

#: Qt 直接依赖的 ICU 模块名。icuin（国际化）与 icuuc（通用）是一对。
ICU_MODULES = ("icuuc.dll", "icuin.dll")

_SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:Windows")) / "System32"

#: 只需要修一次。import 顺序决定了这件事必须发生在最前面，
#: 所以用模块级标志而不是让每个调用方自己记。
_done = False

#: 上一次 ensure() 是否真的做了修补。供测试与诊断读取。
last_result: str = "未运行"


def ensure_qt_runtime() -> bool:
    """在 import PySide6.QtCore **之前**调用。

    返回是否做了修补。可以重复调用；非 Windows 上直接返回 False。
    """
    global _done, last_result
    if _done:
        return False
    _done = True

    if sys.platform != "win32":
        last_result = "非 Windows，无需处理"
        return False

    try:
        import PySide6  # 只 import 包本身，不会加载 QtCore
    except ImportError:
        last_result = "未安装 PySide6"
        return False

    bundled_dir = Path(PySide6.__file__).resolve().parent
    bundled = [name for name in ICU_MODULES if (bundled_dir / name).exists()]
    if bundled:
        last_result = f"PySide6 自带 {bundled}，不插手"
        log.debug("PySide6 自带 ICU（%s），跳过预加载", ", ".join(bundled))
        return False

    preloaded: list[str] = []
    for name in ICU_MODULES:
        candidate = _SYSTEM32 / name
        if not candidate.exists():
            continue
        try:
            ctypes.WinDLL(str(candidate))
            preloaded.append(name)
        except OSError as exc:  # 预加载失败不是致命错误：让它照原样失败
            log.debug("预加载 %s 失败：%s", candidate, exc)

    if preloaded:
        last_result = "已预加载 " + ", ".join(preloaded)
        log.info(
            "已预加载系统 ICU（%s）：PySide6 未自带 ICU，"
            "而 conda 的 Library\\bin 里的旧 ICU 会抢走 ucnv_* 符号",
            ", ".join(preloaded),
        )
        return True

    last_result = "系统里找不到可用的 ICU"
    log.warning("PySide6 未自带 ICU，系统 ICU 也不可用，Qt 可能加载失败")
    return False
