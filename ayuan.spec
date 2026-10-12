# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller 打包配置。

用法：
    .venv\Scripts\pyinstaller.exe ayuan.spec --noconfirm

## 为什么用 .spec 而不是一长串命令行参数

打包参数会变（加依赖、减资源），而命令行参数散在文档或某个人的终端历史里
迟早失传。spec 是**入库的、可复现的**构建定义 —— 谁 clone 下来都能打出
同样的包，出问题也能对着它查。

## 只打进真正会读的模型文件

models/ 一共 70MB，但 wakeword.py 只读 4 个文件：tokens + encoder +
decoder + joiner（chunk-16 那一组）。剩下的 chunk-8 变体、int8 变体、
测试音频、发音词典**一个都不读** —— 全打进去是白白多出 58MB。
"""

import ctypes
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


def _preload_system_icu() -> None:
    r"""在分析开始之前，把系统的 ICU 拉进构建进程。

    ## 为什么这行不能省

    PySide6 6.12 的 wheel **不自带 ICU**，而 conda 会在 DLL 搜索路径里
    塞进 `Libraryin`，那里那份 ICU 只导出带版本后缀的符号
    （`ucnv_open_73`），Qt6Core 要的是无后缀的 `ucnv_open` ——
    于是 `import PySide6.QtCore` 直接失败。详见 app/ui/qt_runtime.py。

    PyInstaller 的 Qt 支持**要靠 import QtCore 去问 QLibraryInfo
    「插件在哪」**。这个 import 一失败，它就收不到任何 Qt 插件 ——
    而**它不报错**，只是安静地少收一批文件。结果是打出来的包一启动就死在：

        qt.qpa.plugin: Could not find the Qt platform plugin "windows"

    这个坑实测踩过：包里连一个 qwindows.dll 都没有，而构建日志全是
    "completed successfully"。所以修在源头 —— 让分析期也能 import QtCore。
    """
    if sys.platform != "win32":
        return
    system32 = Path(os.environ.get("SystemRoot", r"C:Windows")) / "System32"
    for name in ("icuuc.dll", "icuin.dll"):
        candidate = system32 / name
        if not candidate.exists():
            continue
        try:
            ctypes.WinDLL(str(candidate))
        except OSError:
            pass


_preload_system_icu()

#: 调试版带控制台。
#:
#: 正式包是 windowed（console=False），双击不弹黑框 —— 但这也意味着
#: 启动阶段出问题时**什么都看不见**：PyInstaller 的错误框一关就没了，
#: 日志文件也可能还没建起来。
#:
#:     $env:AYUAN_BUILD_CONSOLE='1'; pyinstaller ayuan.spec --noconfirm
#:
#: 打包排障时先用它跑一遍，看到真实输出再切回正式包。
CONSOLE = os.environ.get("AYUAN_BUILD_CONSOLE") == "1"

KWS_DIR = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"

#: 唤醒模型里真正会被读到的文件（见 app/perception/wakeword.py 的
#: WakeWordConfig.tokens / encoder() / decoder() / joiner()）
KWS_FILES = (
    "tokens.txt",
    "encoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    "joiner-epoch-13-avg-2-chunk-16-left-64.onnx",
)

#: Qt 插件必须**显式收集**，不能指望 PyInstaller 自动探测。
#:
#: 它靠 import QtCore 去问 QLibraryInfo「插件在哪」，而在这个 conda 环境里
#: 那个 import 会失败（见 _preload_system_icu 的说明），失败时它只是
#: 安静地少收一批文件。实测踩过：包里连一个 qwindows.dll 都没有。
QT_PLUGIN_KINDS = ("platforms", "styles", "imageformats", "iconengines")


def _collect_qt_plugins() -> list:
    """把 PySide6 自带的插件按 Qt 期望的目录结构收进来。

    PySide6 的 Qt DLL 会被放到 _internal/PySide6/，Qt 由 DLL 位置反推
    「前缀」，于是它会去 _internal/PySide6/plugins 找插件 ——
    所以目标路径必须写成 PySide6/plugins/<类别>/。
    """
    import PySide6

    root = Path(PySide6.__file__).resolve().parent / "plugins"
    collected = []
    for kind in QT_PLUGIN_KINDS:
        source = root / kind
        if not source.is_dir():
            continue
        for dll in sorted(source.glob("*.dll")):
            collected.append((str(dll), f"PySide6/plugins/{kind}"))
    return collected


datas = _collect_qt_plugins()
datas += [(f"models/{KWS_DIR}/{name}", f"models/{KWS_DIR}") for name in KWS_FILES]
datas += [
    ("models/keywords_ayuan.txt", "models"),
    ("models/keywords_stop.txt", "models"),
    # 给用户照着填的模板。
    # 注意：**不打进 .env** —— 那里面是密钥，分发包里不能有它。
    (".env.example", "."),
]

binaries = []
hiddenimports = []

# 这几个包带自己的二进制或数据文件（PortAudio、onnxruntime、VAD 的 pyd），
# 光靠静态分析收不全。
for package in ("sherpa_onnx", "sounddevice", "_sounddevice_data", "webrtcvad"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 — 少收一个也要能继续，日志里看得见
        print(f"[spec] 收集 {package} 失败，跳过：{exc}")
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

#: 明确排除用不到的大块头。它们本来就不会被静态分析收进来，
#: 写在这里是为了让体积变化有迹可循。
excludes = [
    "tkinter",
    "matplotlib",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtPdf",
]


a = Analysis(
    ["app/ui/app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # 覆盖掉自带的 hook-webrtcvad（它写死了发行版名 webrtcvad，
    # 而我们装的是 webrtcvad-wheels，构建会直接崩）
    hookspath=["tools/pyinstaller_hooks"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Ayuan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩过的 exe 更容易被杀软误报，不值那点体积
    console=CONSOLE,    # 正式包不弹黑框；日志写到 data/ayuan.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# 自检：Qt 平台插件必须真的被收进来。
# 少了它，包能构建成功、能产出 exe，却一启动就死 —— 而构建日志里
# 一句错都不会有。宁可在构建阶段就炸，也不要发一个跑不起来的包。
_platform_plugins = [
    entry for entry in list(a.binaries) + list(a.datas) if "platforms" in str(entry[0])
]
if not _platform_plugins:
    raise SystemExit(
        "[spec] 没有收到任何 Qt 平台插件（platforms/qwindows.dll）。\n"
        "        这个包打出来会一启动就死在 "
        "'Could not find the Qt platform plugin'。\n"
        "        多半是分析期 import PySide6.QtCore 失败了 —— "
        "检查 _preload_system_icu() 有没有生效。"
    )
print(f"[spec] 已收集 Qt 平台插件 {len(_platform_plugins)} 个")


coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Ayuan",
)
