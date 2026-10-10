"""配置层测试（无环境依赖，可注入）。"""

from __future__ import annotations

import pytest

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
