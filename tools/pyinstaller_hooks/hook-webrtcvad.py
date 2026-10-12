"""覆盖 PyInstaller 自带的 hook-webrtcvad。

## 为什么要覆盖

自带的那个是一行 `datas = copy_metadata('webrtcvad')`，而我们装的是
**webrtcvad-wheels** —— 发行版名字不一样，`importlib.metadata` 找不到，
于是整个构建崩在：

    ImportErrorWhenRunningHook: ERROR: Failed to import module
    __PyInstaller_hooks_0_webrtcvad required by hook for module ...
    hook-webrtcvad.py

报错信息完全没提「包名不对」，排查时容易被带偏到「是不是 webrtcvad 装坏了」
（实测 `import webrtcvad` 是好的）。

这个包名不一致是真实存在的：PyPI 上的 wheel 叫 webrtcvad-wheels，
但 import 名仍是 webrtcvad —— requirements.txt 里也专门记了这一点。

## 为什么不干脆把它 exclude 掉

`copy_metadata` 只是拷一份 dist-info，供运行时代码查版本号用。
理论上不拷也行，但**保留是对的**：万一将来某处 `importlib.metadata.version("webrtcvad")`
（或 sherpa-onnx 内部这么做），少了它就会在打包后、用户机器上才炸。
"""

from PyInstaller.utils.hooks import copy_metadata

try:
    datas = copy_metadata("webrtcvad-wheels")
except Exception:  # noqa: BLE001 — 拿不到元数据不该让打包整个失败
    datas = []
