"""配置层测试（无环境依赖，可注入）。"""

from __future__ import annotations

import pytest

from app import config as config_module
from app.config import AudioSettings, Config, ModelSettings, Secrets, mask_secret


class TestMaskSecret:
    """密钥脱敏。"""

    def test_empty(self) -> None:
        assert mask_secret("") == "未配置"

    def test_never_leaks_body(self) -> None:
        """回归：曾经打印首 8 位 + 末 4 位。

        12 位明文在 117 位密钥上影响有限，但 DashScope 常见 35 位密钥
        —— 露 12 位就是 34%。而自检输出正是用户求助时最爱截图的内容。
        """
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        masked = mask_secret(secret)
        assert "abcdefgh" not in masked, "不得泄露密钥正文"
        assert "012345" not in masked, "不得泄露密钥尾部"
        assert masked.count("*") >= 3

    def test_keeps_prefix_only(self) -> None:
        assert mask_secret("sk-1234567890").startswith("sk-")

    def test_non_sk_prefix_is_hidden(self) -> None:
        masked = mask_secret("AKIDabcdefg")
        assert "AKID" not in masked, "非 sk- 前缀不应被当作可公开信息"


class TestAudioSettings:
    """值对象必须纯净，否则测试不可复现。"""

    def test_defaults_are_deterministic(self) -> None:
        settings = AudioSettings()
        assert settings.input_device == ""
        assert settings.output_device == ""
        assert settings.record_seconds == 3.0
        assert settings.sample_rate == 16000

    def test_from_env_injection(self) -> None:
        settings = AudioSettings.from_env(
            {"AYUAN_RECORD_SECONDS": "7.5", "AYUAN_INPUT_DEVICE": "麦克风阵列"}
        )
        assert settings.record_seconds == 7.5
        assert settings.input_device == "麦克风阵列"

    def test_invalid_number_falls_back(self) -> None:
        """非法值回落（并留痕），不能崩。"""
        assert AudioSettings.from_env({"AYUAN_RECORD_SECONDS": "3s"}).record_seconds == 3.0

    def test_empty_env_uses_defaults(self) -> None:
        assert AudioSettings.from_env({}).record_seconds == 3.0


class TestDerivedValues:
    def test_frame_samples(self) -> None:
        assert AudioSettings().frame_samples == 320  # 16000 * 20ms

    def test_frame_bytes(self) -> None:
        assert AudioSettings().frame_bytes == 640  # 320 * 2字节 * 1声道


class TestModelSettings:
    def test_thinking_disabled_by_default(self) -> None:
        """首字延迟 0.46s vs 4.46s —— 这个必须为 False。"""
        assert ModelSettings().enable_thinking is False

    def test_from_env_override(self) -> None:
        model = ModelSettings.from_env({"AYUAN_LLM": "qwen3.8-flash"})
        assert model.llm == "qwen3.8-flash"
        assert model.tts == ModelSettings().tts  # 未覆盖的项保持默认


class TestSecrets:
    def test_empty_by_default(self) -> None:
        secrets = Secrets()
        assert not secrets.has_dashscope
        assert not secrets.has_weather

    def test_from_env(self) -> None:
        secrets = Secrets.from_env({"DASHSCOPE_API_KEY": "sk-test"})
        assert secrets.has_dashscope
        assert not secrets.has_weather


class TestConfigLoad:
    def test_injectable(self) -> None:
        config = Config.load({"DASHSCOPE_API_KEY": "sk-x", "AYUAN_LOG_LEVEL": "debug"})
        assert config.secrets.dashscope_api_key == "sk-x"
        assert config.log_level == "DEBUG"

    def test_empty_env(self) -> None:
        config = Config.load({})
        assert not config.secrets.has_dashscope
        assert config.log_level == "INFO"


# ══════════════════════════════════════════════════════
#  打包后的路径（M5-5）
# ══════════════════════════════════════════════════════

class TestFrozenPaths:
    """打包成 exe 之后，`__file__` 指向包内部的 _internal 目录。

    让用户把 .env 放进那里等于让他去拆包。所以冻结时**用户根**改用
    可执行文件所在目录，而**资源根**（随包分发的模型）仍在 _MEIPASS。
    两个根分开，是 M5-5 打包时发现必须做的事。
    """

    def test_dev_mode_uses_one_root(self) -> None:
        assert config_module._user_root() == config_module._resource_root()
        assert config_module._resource_root() == config_module.PROJECT_ROOT

    def test_not_frozen_by_default(self) -> None:
        assert config_module._is_frozen() is False

    def test_frozen_splits_the_two_roots(self, monkeypatch, tmp_path) -> None:
        exe = tmp_path / "Ayuan"
        internal = tmp_path / "_internal"
        exe.mkdir()
        internal.mkdir()
        monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(config_module.sys, "executable", str(exe / "Ayuan.exe"))
        monkeypatch.setattr(config_module.sys, "_MEIPASS", str(internal), raising=False)

        assert config_module._is_frozen() is True
        assert config_module._user_root() == exe, ".env 要放在 exe 旁边，用户才找得到"
        assert config_module._resource_root() == internal, "模型是随包分发的资源"

    def test_frozen_without_meipass_falls_back_to_the_exe_dir(
        self, monkeypatch, tmp_path
    ) -> None:
        """_MEIPASS 不在时也不能崩 —— 退回 exe 目录总比抛异常好。"""
        monkeypatch.setattr(config_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(config_module.sys, "executable", str(tmp_path / "Ayuan.exe"))
        monkeypatch.delattr(config_module.sys, "_MEIPASS", raising=False)

        assert config_module._resource_root() == tmp_path
        assert config_module._user_root() == tmp_path

    def test_models_dir_comes_from_the_resource_root(self) -> None:
        assert config_module.MODELS_DIR.name == "models"
        assert config_module.MODELS_DIR.parent == config_module._resource_root()

    def test_env_file_comes_from_the_user_root(self) -> None:
        assert config_module.ENV_FILE.name == ".env"
        assert config_module.ENV_FILE.parent == config_module._user_root()