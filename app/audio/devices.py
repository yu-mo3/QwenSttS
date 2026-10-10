"""音频设备枚举与选择。

存在的意义：这台机器上同时有「麦克风阵列」「蓝牙耳机免提」「虚拟声卡」等
多个输入输出端点，直接让 sounddevice 用系统默认值，会出现「对着蓝牙耳机
说话，但录进去的是内置麦克风」这类玄学问题。

策略：用「关键字模糊匹配」选中目标设备，匹配不到就回落到系统默认，
并把最终选择打印出来，让问题可见。
"""

from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd

from app.core.errors import AudioDeviceError
from app.core.logger import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class AudioDevice:
    """一个音频端点。"""

    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    hostapi: str

    @property
    def is_input(self) -> bool:
        return self.max_input_channels > 0

    @property
    def is_output(self) -> bool:
        return self.max_output_channels > 0

    @property
    def kind(self) -> str:
        if self.is_input and self.is_output:
            return "双向"
        return "输入" if self.is_input else "输出"

    def __str__(self) -> str:
        return f"[{self.index:>2}] {self.kind} · {self.name}"


def list_devices() -> list[AudioDevice]:
    """枚举全部音频端点。"""
    try:
        raw = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:  # PortAudio 初始化失败
        raise AudioDeviceError(
            "无法枚举音频设备",
            hint=f"PortAudio 报错：{exc}",
        ) from exc

    devices: list[AudioDevice] = []
    for index, info in enumerate(raw):
        api_index = info.get("hostapi", 0)
        api_name = hostapis[api_index]["name"] if api_index < len(hostapis) else "?"
        devices.append(
            AudioDevice(
                index=index,
                name=str(info["name"]),
                max_input_channels=int(info["max_input_channels"]),
                max_output_channels=int(info["max_output_channels"]),
                default_samplerate=float(info["default_samplerate"]),
                hostapi=api_name,
            )
        )
    return devices


def resolve_device(keyword: str, *, want_input: bool) -> int | None:
    """按关键字找设备索引。

    匹配规则：关键字为空 → 返回 None（交给 sounddevice 用系统默认）；
    否则做「不区分大小写的子串匹配」，优先选端点方向正确的设备。

    为什么不用「完全相等」：设备名会随驱动更新变化（例如插入蓝牙耳机后
    名字变成「耳机 (Baseus Bass BF1 Lite)」），子串匹配更耐用。
    """
    if not keyword:
        return None

    candidates = [
        dev for dev in list_devices()
        if (dev.is_input if want_input else dev.is_output)
    ]
    needle = keyword.lower()

    exact = [dev for dev in candidates if dev.name.lower() == needle]
    if exact:
        log.debug("设备关键字 %r 精确命中：%s", keyword, exact[0].name)
        return exact[0].index

    fuzzy = [dev for dev in candidates if needle in dev.name.lower()]
    if fuzzy:
        chosen = fuzzy[0]
        log.info("设备关键字 %r 命中：%s", keyword, chosen.name)
        return chosen.index

    log.warning(
        "设备关键字 %r 未命中任何%s设备，回落到系统默认",
        keyword, "输入" if want_input else "输出",
    )
    return None


def default_device(*, want_input: bool) -> AudioDevice | None:
    """取系统默认输入/输出设备。"""
    try:
        index = sd.default.device[0 if want_input else 1]
    except Exception:
        return None
    if index is None or index < 0:
        return None
    for dev in list_devices():
        if dev.index == index:
            return dev
    return None


def describe_devices() -> str:
    """生成一段人类可读的设备清单，供自检脚本打印。"""
    devices = list_devices()
    if not devices:
        return "（未发现任何音频设备）"

    lines = []
    for dev in devices:
        marker = ""
        if dev.name == (default_device(want_input=dev.is_input).name if default_device(want_input=dev.is_input) else ""):
            marker = "  ← 系统默认"
        lines.append(f"{dev}{marker}")
    return "\n".join(lines)
