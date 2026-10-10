"""音频设备枚举、选择与路由推荐。

存在的意义：一台机器上同时有「麦克风阵列」「蓝牙耳机免提」「虚拟声卡」等
多个端点，直接让 sounddevice 用系统默认值，会出现「对着蓝牙耳机说话，
但录进去的是内置麦克风」这类玄学问题。

本模块持有**设备知识**（免提模式规避、宿主接口折叠、关键字匹配策略），
供 check_env / main / 编排层共用 —— 这些是业务知识，不是诊断脚本的私货。
"""

from __future__ import annotations

from dataclasses import dataclass

import sounddevice as sd

from app.core.errors import AudioDeviceError
from app.core.logger import get_logger

log = get_logger(__name__)

# sounddevice 在 Windows 上的默认宿主接口。同一个物理设备会在 MME /
# DirectSound / WASAPI / WDM-KS 下各出现一次（实测 26 个端点里只有 6 个
# 属于 MME），展示时只保留这一组才不会淹没用户。
PREFERRED_HOSTAPI = "MME"

# 蓝牙「免提」端点的特征。一旦启用，带宽骤降、音质掉到电话级别 ——
# 这是蓝牙协议本身的限制（HFP vs A2DP），绕不过去，只能主动避开。
HANDSFREE_MARKERS = ("hands-free", "免提", "bthhfenum")

# 挑选内置麦克风时优先匹配的名字片段 —— 这类设备通常是笔记本自带的阵列，
# 与蓝牙耳机在物理上分离，最不容易产生回声
BUILTIN_MIC_MARKERS = ("麦克风阵列", "microphone array", "麦克风 (")


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

    @property
    def is_handsfree(self) -> bool:
        """是否为蓝牙免提端点（音质差，应尽量避开）。"""
        return is_handsfree(self.name)

    def __str__(self) -> str:
        return f"[{self.index:>2}] {self.kind} · {self.name}"


def is_handsfree(name: str) -> bool:
    """判断设备名是否属于蓝牙免提端点。"""
    lowered = name.lower()
    return any(marker in lowered for marker in HANDSFREE_MARKERS)


# ══════════════════════════════════════════════════════════
#  枚举
# ══════════════════════════════════════════════════════════

def list_devices(hostapi: str | None = None) -> list[AudioDevice]:
    """枚举音频端点。

    Args:
        hostapi: 只保留指定宿主接口（例如 "MME"）；None 表示全部。
    """
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
        if hostapi is not None and api_name != hostapi:
            continue
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


def inputs(devices: list[AudioDevice] | None = None) -> list[AudioDevice]:
    """筛出输入设备。"""
    return [d for d in (devices if devices is not None else list_devices()) if d.is_input]


def outputs(devices: list[AudioDevice] | None = None) -> list[AudioDevice]:
    """筛出输出设备。"""
    return [d for d in (devices if devices is not None else list_devices()) if d.is_output]


# ══════════════════════════════════════════════════════════
#  选择
# ══════════════════════════════════════════════════════════

def resolve_device(keyword: str, *, want_input: bool) -> int | None:
    """按关键字找设备索引。

    匹配规则：关键字为空 → 返回 None（交给 sounddevice 用系统默认）；
    否则做「不区分大小写的子串匹配」。

    为什么不用「完全相等」：设备名会随驱动更新变化（例如插入蓝牙耳机后
    名字从「耳机」变成「耳机 (Baseus Bass BF1 Lite)」），子串匹配更耐用。
    """
    if not keyword:
        return None

    candidates = inputs() if want_input else outputs()
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


def default_index(*, want_input: bool) -> int | None:
    """系统默认输入/输出设备的索引。"""
    try:
        index = sd.default.device[0 if want_input else 1]
    except Exception:
        return None
    if index is None or index < 0:
        return None
    return int(index)


def default_device(*, want_input: bool) -> AudioDevice | None:
    """系统默认输入/输出设备。"""
    index = default_index(want_input=want_input)
    if index is None:
        return None
    for dev in list_devices():
        if dev.index == index:
            return dev
    return None


def resolve_name(index: int | None, *, want_input: bool) -> str:
    """把设备索引翻译成人类可读的名字。

    index 为 None 时回落到系统默认；都拿不到时返回一句可读的说明。

    这是 capture.py 与 player.py 里重复了两遍的方法，收敛到这里。
    """
    effective = index if index is not None else default_index(want_input=want_input)
    if effective is None:
        return "（无可用输入设备）" if want_input else "（无可用输出设备）"
    try:
        return str(sd.query_devices(effective)["name"])
    except Exception:
        return f"设备 #{effective}"


# ══════════════════════════════════════════════════════════
#  路由推荐
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class DeviceRoute:
    """推荐的输入 / 输出组合。"""

    input_device: AudioDevice | None
    output_device: AudioDevice | None
    reason: str

    @property
    def uses_handsfree(self) -> bool:
        return bool(
            (self.input_device and self.input_device.is_handsfree)
            or (self.output_device and self.output_device.is_handsfree)
        )


def suggest_route(devices: list[AudioDevice] | None = None) -> DeviceRoute:
    """推荐一套「听得清、放得好」的设备组合。

    核心策略：**让内置麦克风收音、耳机只负责放音**。

    原因：蓝牙耳机一旦兼任麦克风，Windows 会把它切到 Hands-Free 模式，
    带宽骤降、音质掉到电话级别 —— 而麦克风和输出本来是两条独立链路，
    没有任何理由让耳机同时承担。

    优先级：
      输入 → 内置麦克风阵列 > 非免提设备 > 系统默认 > 第一个可用设备
      输出 → 非免提的耳机 > 非免提设备 > 系统默认 > 第一个可用设备
    """
    devs = devices if devices is not None else list_devices()
    ins = [d for d in devs if d.is_input]
    outs = [d for d in devs if d.is_output]

    mic = _pick(ins, BUILTIN_MIC_MARKERS)
    if mic is None:
        mic = _pick(ins, avoid_handsfree=True)
    if mic is None:
        mic = default_device(want_input=True) or (ins[0] if ins else None)

    speaker = _pick(outs, ("耳机", "headphone"), avoid_handsfree=True)
    if speaker is None:
        speaker = _pick(outs, avoid_handsfree=True)
    if speaker is None:
        speaker = default_device(want_input=False) or (outs[0] if outs else None)

    reasons = []
    if mic and mic.is_handsfree:
        reasons.append("输入设备处于免提模式，音质会下降")
    else:
        reasons.append("内置麦克风收音，避开蓝牙免提降质")
    if speaker and speaker.is_handsfree:
        reasons.append("输出设备处于免提模式，音质会下降")
    else:
        reasons.append("耳机只负责放音，两条链路互不干扰")

    return DeviceRoute(input_device=mic, output_device=speaker, reason="；".join(reasons))


def _pick(
    candidates: list[AudioDevice],
    markers: tuple[str, ...] | None = None,
    *,
    avoid_handsfree: bool = False,
) -> AudioDevice | None:
    """从候选里挑第一个满足条件的设备。"""
    for dev in candidates:
        if avoid_handsfree and dev.is_handsfree:
            continue
        if markers:
            lowered = dev.name.lower()
            if not any(m.lower() in lowered for m in markers):
                continue
        return dev
    return None
