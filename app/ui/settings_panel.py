"""设置面板。

## 三类改动，三种生效方式

界面上必须把这件事说清楚，否则用户会以为「点一下马上就换好了」：

    唤醒 / 停阈值、静默超时   纯数值，**立即生效**
    输入 / 输出设备           要重开音频流，**必须重开会话**
    保存到 .env               只有显式点按钮才写盘

## 为什么不自动保存

.env 里有用户的密钥。静默改写它意味着「我只是拖了一下滑块，
密钥文件就变了」—— 一旦写坏，用户不会想到是界面干的。
所以：任何持久化都必须是一次明确的点击。

写入本身是**行级**的（见 config.update_env_file）：只动我们拥有的那几个键，
其它行、注释、别人工具用的键一律原样保留。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import ENV_FILE, Config, update_env_file
from app.core.logger import get_logger
from app.ui import theme
from app.ui.controller import SessionController

log = get_logger(__name__)

#: 阈值输入范围。实测能触发的区间是 0.01~0.2，但界面不该把这个上限
#: 写死成「不可能更大」—— 换模型后区间会变，所以给到 1.0。
THRESHOLD_MIN = 0.01
THRESHOLD_MAX = 1.0

IDLE_MIN = 5
IDLE_MAX = 600


class SettingsDialog(QDialog):
    """设置。改完点「关闭」即可，各改动按上面的规则各自生效。"""

    def __init__(
        self,
        config: Config,
        controller: SessionController,
        parent: QWidget | None = None,
        *,
        env_path=None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.controller = controller
        # 写盘目标可注入：测试必须能指向一个临时文件。
        # 「测试跑一遍就把用户的 .env 改了」是绝对不能接受的事故。
        self.env_path = env_path

        self.setWindowTitle("设置")
        self.setMinimumWidth(460)
        self._loading = True          # 填初值期间不要触发「立即生效」

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        layout.setSpacing(theme.GAP)

        layout.addWidget(self._build_device_section())
        layout.addWidget(self._build_tuning_section())
        layout.addWidget(self._build_persist_section())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._load_current()
        self._loading = False

    # ══════════════════════════════════════════════════════
    #  装配
    # ══════════════════════════════════════════════════════

    def _section(self, title: str) -> tuple[QFrame, QVBoxLayout]:
        box = QFrame()
        box.setObjectName("Panel")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(theme.PAD, theme.PAD, theme.PAD, theme.PAD)
        outer.setSpacing(8)
        caption = QLabel(title)
        caption.setObjectName("Title")
        outer.addWidget(caption)
        return box, outer

    def _build_device_section(self) -> QWidget:
        box, outer = self._section("音频设备")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.input_combo = QComboBox()
        self.input_combo.setEditable(True)
        self.input_combo.setToolTip("留空表示用系统默认设备。可以直接输入设备名的一部分。")
        form.addRow("输入设备", self.input_combo)

        self.output_combo = QComboBox()
        self.output_combo.setEditable(True)
        self.output_combo.setToolTip("留空表示用系统默认设备。")
        form.addRow("输出设备", self.output_combo)
        outer.addLayout(form)

        hint = QLabel("换设备要重新打开音频流，所以必须重开会话才生效。")
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self.apply_devices_button = QPushButton("应用设备并重开会话")
        self.apply_devices_button.clicked.connect(self.on_apply_devices)
        outer.addWidget(self.apply_devices_button)

        self._populate_devices()
        return box

    def _build_tuning_section(self) -> QWidget:
        box, outer = self._section("感知参数")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.wake_spin = QDoubleSpinBox()
        self.wake_spin.setRange(THRESHOLD_MIN, THRESHOLD_MAX)
        self.wake_spin.setSingleStep(0.05)
        self.wake_spin.setDecimals(2)
        self.wake_spin.setToolTip(
            "唤醒词「阿源」的判定阈值。实测可触发区间 0.01~0.2，越高越不容易误触发。"
        )
        self.wake_spin.valueChanged.connect(self.on_tuning_changed)
        form.addRow("唤醒阈值", self.wake_spin)

        self.stop_spin = QDoubleSpinBox()
        self.stop_spin.setRange(THRESHOLD_MIN, THRESHOLD_MAX)
        self.stop_spin.setSingleStep(0.05)
        self.stop_spin.setDecimals(2)
        self.stop_spin.setToolTip("「停」的判定阈值。留到与唤醒阈值相同即可。")
        self.stop_spin.valueChanged.connect(self.on_tuning_changed)
        form.addRow("停阈值", self.stop_spin)

        self.idle_spin = QSpinBox()
        self.idle_spin.setRange(IDLE_MIN, IDLE_MAX)
        self.idle_spin.setSuffix(" 秒")
        self.idle_spin.setToolTip("聆听状态下多久没说话就回到待机。")
        self.idle_spin.valueChanged.connect(self.on_tuning_changed)
        form.addRow("静默超时", self.idle_spin)

        outer.addLayout(form)

        hint = QLabel("这三项都是纯数值，改完立即生效，不用重开会话。")
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        outer.addWidget(hint)
        return box

    def _build_persist_section(self) -> QWidget:
        box, outer = self._section("保存")

        hint = QLabel(
            "把上面的设备与感知参数写回 .env，下次启动仍然有效。\n"
            "只改写这几项，文件里的其它内容与注释原样保留；"
            "密钥不会被读到界面上，也不会被改动。"
        )
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        row = QWidget()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(theme.GAP)

        self.save_button = QPushButton("保存到 .env")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self.on_save)
        line.addWidget(self.save_button)

        self.revert_button = QPushButton("恢复当前值")
        self.revert_button.setToolTip("把上面的输入框改回正在生效的值（不写盘）")
        self.revert_button.clicked.connect(lambda: self._load_current())
        line.addWidget(self.revert_button)
        line.addStretch(1)

        outer.addWidget(row)
        return box

    # ══════════════════════════════════════════════════════
    #  初值
    # ══════════════════════════════════════════════════════

    def _populate_devices(self) -> None:
        """把设备列进下拉框。

        枚举失败（没有音频硬件的机器）时退化成一个纯输入框 ——
        设置面板打不开比下拉框是空的更糟。
        """
        try:
            from app.audio.devices import list_devices

            devices = list_devices()
        except Exception as exc:  # noqa: BLE001
            log.warning("枚举音频设备失败，设备框退化为纯文本输入：%s", exc)
            devices = []

        self.input_combo.addItem("", "")
        self.output_combo.addItem("", "")
        for device in devices:
            label = f"[{device.index}] {device.name}"
            if device.is_handsfree:
                label += "  ← 免提·音质差"
            if device.is_input:
                self.input_combo.addItem(label, device.name)
            if device.is_output:
                self.output_combo.addItem(label, device.name)

    def _select_device(self, combo: QComboBox, keyword: str) -> None:
        combo.setCurrentText(keyword)
        if not keyword:
            return
        for index in range(combo.count()):
            if combo.itemData(index) == keyword:
                combo.setCurrentIndex(index)
                return

    def _load_current(self) -> None:
        self._loading = True
        try:
            audio = self.controller.config.audio
            self._select_device(self.input_combo, audio.input_device)
            self._select_device(self.output_combo, audio.output_device)

            perception = self.controller.config.perception
            wake = (
                self.controller.wake_threshold
                if self.controller.wake_threshold is not None
                else perception.wake_threshold
            )
            stop = (
                self.controller.stop_threshold
                if self.controller.stop_threshold is not None
                else perception.effective_stop_threshold
            )
            self.wake_spin.setValue(wake)
            self.stop_spin.setValue(stop)
            self.idle_spin.setValue(int(self.controller.idle_timeout))
        finally:
            self._loading = False

    # ══════════════════════════════════════════════════════
    #  动作
    # ══════════════════════════════════════════════════════

    def _device_keyword(self, combo: QComboBox) -> str:
        """取用户选的设备关键字。

        用它存的是**设备名**而不是索引：Windows 的设备索引会漂移，
        存索引下次启动可能指到另一台设备上（对着耳机说话却录进内置麦）。
        """
        data = combo.currentData()
        if isinstance(data, str) and data and combo.currentText() == combo.itemText(combo.currentIndex()):
            return data
        return combo.currentText().strip()

    def on_apply_devices(self) -> None:
        input_device = self._device_keyword(self.input_combo)
        output_device = self._device_keyword(self.output_combo)
        self.controller.apply_devices(
            input_device=input_device, output_device=output_device
        )
        QMessageBox.information(
            self,
            "已应用",
            "设备已切换，会话正在重开。\n唤醒词模型要重新加载，请稍等一两秒。",
        )

    def on_tuning_changed(self) -> None:
        if self._loading:
            return          # 填初值时不该顺手把设置改一遍
        self.controller.set_thresholds(
            wake=self.wake_spin.value(), stop=self.stop_spin.value()
        )
        self.controller.set_idle_timeout(self.idle_spin.value())

    def on_save(self) -> None:
        updates = {
            "AYUAN_INPUT_DEVICE": self._device_keyword(self.input_combo),
            "AYUAN_OUTPUT_DEVICE": self._device_keyword(self.output_combo),
            "AYUAN_WAKE_THRESHOLD": f"{self.wake_spin.value():.2f}",
            "AYUAN_STOP_THRESHOLD": f"{self.stop_spin.value():.2f}",
            "AYUAN_IDLE_TIMEOUT": f"{self.idle_spin.value():.0f}",
        }
        try:
            path = update_env_file(updates, self.env_path)
        except OSError as exc:
            target = self.env_path or ENV_FILE
            QMessageBox.warning(
                self,
                "保存失败",
                f"写不了 {target.name}：{exc}\n请检查文件是否被占用或只读。",
            )
            return
        QMessageBox.information(
            self,
            "已保存",
            f"设置已写入 {path.name}。\n下次启动阿源时生效。",
        )
