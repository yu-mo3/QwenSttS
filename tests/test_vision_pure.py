"""M5 视觉链路的纯函数测试。

**不需要摄像头、不需要网络、不需要密钥。** 摄像头相关的部分只测纯函数
（缩放、编码），真实抓帧由 tools/check_camera.py 人工跑 ——
它必须真的碰硬件，不适合放进自动化测试。
"""

from __future__ import annotations

import numpy as np
import pytest

from app.capabilities.vlm import (
    VisionResult,
    detect_mime,
    parse_description,
    to_data_uri,
)
from app.camera import encode_jpeg, scale_to_fit
from app.config import CameraSettings, Config
from app.core.errors import ApiError, CameraError
from app.orchestrator.vision_intent import (
    NOT_VISION_SUFFIXES,
    is_vision_request,
    strip_trigger,
    with_vision_context,
)

JPEG_MAGIC = b"\xff\xd8\xff"


def solid(height: int, width: int, value: int = 128) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


# ══════════════════════════════════════════════════════
#  视觉意图
# ══════════════════════════════════════════════════════

class TestVisionIntent:
    """误触发的代价不对称（多拍一张 vs 她答非所问），所以规则偏宽松，
    但宽松要有边界 —— 下面每一条边界都是被真实误判逼出来的。"""

    @pytest.mark.parametrize(
        "text",
        [
            "看看这是什么",
            "这是什么",
            "你看看",
            "帮我看看",
            "看看我手里是什么",
            "看看这个零件有没有裂纹",
            "看看这个",
            "这是什么东西",
            "我手里是什么",
            "帮我看看，这是什么",
        ],
    )
    def test_recognises_vision_requests(self, text):
        assert is_vision_request(text), f"{text!r} 应当被识别为要看一眼"

    @pytest.mark.parametrize(
        "text",
        [
            "明天天气怎么样",
            "你好呀",
            "今天几号",
            "我觉得这个问题很有意思",
            "",
            "   ",
        ],
    )
    def test_rejects_ordinary_chat(self, text):
        assert not is_vision_request(text), f"{text!r} 不该被当成要看一眼"

    @pytest.mark.parametrize(
        "text",
        [
            "这是什么意思",
            "这是什么时候的事",
            "这是什么原因造成的",
        ],
    )
    def test_rejects_what_does_this_mean(self, text):
        """「这是什么 X」在问别的东西时不能拍照。

        最初按「整句够短就算」判定，这几句全被判成要看 ——
        「这是什么意思」才 6 个字，短句规则根本挡不住。
        """
        assert not is_vision_request(text), f"{text!r} 是在问含义，不是要看"

    def test_rejects_opinion_asking(self):
        """「我觉得你看这个方案怎么样」是在征求意见。

        它含「你看」—— 而那是个话语标记（「你看，我说得对吧」），
        不该当强信号。所以 LOOK_VERBS 里故意没有它。
        """
        assert not is_vision_request("我觉得你看这个方案怎么样")

    def test_suffix_list_is_not_empty(self):
        assert NOT_VISION_SUFFIXES

    def test_strip_only_removes_from_the_start(self):
        """触发词是句首的引导语，不是可以出现在任意位置的记号。

        早先在整句里做替换，把「看看这是什么，这是我新买的杯子」
        删成了「这，这是我新买的杯子」—— 中间那半句里的「这是什么」
        一样被吃掉了。
        """
        assert strip_trigger("看看这是什么，这是我新买的杯子") == "这是我新买的杯子"

    def test_strip_keeps_the_actual_question(self):
        assert strip_trigger("看看这个零件有没有裂纹") == "零件有没有裂纹"

    @pytest.mark.parametrize("text", ["帮我看看这个", "这是什么？", "帮我看看，这是什么"])
    def test_strip_yields_empty_when_there_is_nothing_more(self, text):
        """没有补充说明就该是空串，而不是残留半个字。"""
        assert strip_trigger(text) == ""

    def test_with_vision_context_includes_the_note(self):
        merged = with_vision_context("一只橘猫", "它在干什么")
        assert "一只橘猫" in merged and "它在干什么" in merged

    def test_with_vision_context_without_note(self):
        merged = with_vision_context("一只橘猫")
        assert "一只橘猫" in merged
        assert merged.endswith("）")


# ══════════════════════════════════════════════════════
#  摄像头纯函数
# ══════════════════════════════════════════════════════

class TestScaleToFit:
    def test_small_image_is_returned_untouched(self):
        """小图**原样返回**，不放大。

        放大再送去识别，只会让请求变大，没有任何新信息。
        """
        frame = solid(480, 640)
        assert scale_to_fit(frame, 1024).shape == frame.shape

    def test_large_image_is_scaled_by_the_long_edge(self):
        assert scale_to_fit(solid(1080, 1920), 1024).shape == (576, 1024, 3)

    def test_portrait_is_scaled_by_height(self):
        assert scale_to_fit(solid(1920, 1080), 1024).shape == (1024, 576, 3)

    def test_exact_boundary_is_untouched(self):
        assert scale_to_fit(solid(1024, 1024), 1024).shape == (1024, 1024, 3)

    def test_extreme_max_edge_never_yields_zero_size(self):
        """max_edge=1 时 round 会算出 0 尺寸，而 cv2.resize 对 0 尺寸抛异常 ——
        那会让整条链路以裸异常收场。"""
        assert scale_to_fit(solid(1080, 1920), 1).shape == (1, 1, 3)

    def test_zero_means_no_scaling(self):
        assert scale_to_fit(solid(1080, 1920), 0).shape == (1080, 1920, 3)

    @pytest.mark.parametrize("bad", [np.zeros((0, 0, 3), dtype=np.uint8), None])
    def test_empty_frame_raises_camera_error(self, bad):
        """抛 CameraError 而不是裸 ValueError —— 后者不属于 AyuanError 家族。"""
        with pytest.raises(CameraError):
            scale_to_fit(bad, 1024)


class TestEncodeJpeg:
    def test_produces_a_real_jpeg(self):
        data = encode_jpeg(solid(480, 640))
        assert data.startswith(JPEG_MAGIC)

    def test_respects_max_edge(self):
        import cv2

        data = encode_jpeg(solid(1080, 1920), max_edge=512)
        array = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert max(array.shape[:2]) == 512

    def test_lower_quality_is_smaller(self):
        """用有细节的图测 —— 纯色图在任何质量下都压缩得一样小。"""
        rng = np.random.default_rng(0)
        noisy = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
        assert len(encode_jpeg(noisy, quality=20)) < len(encode_jpeg(noisy, quality=95))

    def test_quality_is_clamped(self):
        """质量给 0 或 999 都不该让 OpenCV 抛。"""
        frame = solid(64, 64)
        assert encode_jpeg(frame, quality=0).startswith(JPEG_MAGIC)
        assert encode_jpeg(frame, quality=999).startswith(JPEG_MAGIC)


# ══════════════════════════════════════════════════════
#  VLM 纯函数
# ══════════════════════════════════════════════════════

class TestImageMime:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"GIF89a", "image/gif"),
            (b"BM", "image/bmp"),
        ],
    )
    def test_detects_by_magic(self, data, expected):
        assert detect_mime(data) == expected

    def test_detects_webp(self):
        assert detect_mime(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"

    def test_unknown_falls_back_to_jpeg(self):
        assert detect_mime(b"\x00\x01\x02\x03") == "image/jpeg"

    def test_data_uri_declares_the_real_type(self):
        """声明与实际不符的 data URI 是在对服务端撒谎。

        多数服务端只是碰巧会嗅探内容才没出错 —— 不能靠这个。
        """
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 32
        assert to_data_uri(png).startswith("data:image/png;base64,")
        assert to_data_uri(b"\xff\xd8\xff" + b"x" * 32).startswith("data:image/jpeg;base64,")

    def test_empty_image_raises_api_error(self):
        with pytest.raises(ApiError):
            to_data_uri(b"")


def _response_type(name: str, **fields):
    """造一个只有指定属性的假响应对象。"""
    return type(name, (), fields)()


def _message_none():
    """造一个 message 为 None 的 choice。"""
    return type("C", (), {"message": None})()


class TestParseDescription:
    class _Message:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = TestParseDescription._Message(content)

    class _Response:
        def __init__(self, choices):
            self.choices = choices

    def _response(self, content):
        return self._Response([self._Choice(content)])

    def test_plain_string(self):
        assert parse_description(self._response(" 一只猫 ")) == "一只猫"

    def test_segmented_content(self):
        response = self._response([{"text": "一只"}, {"text": "橘猫"}])
        assert parse_description(response) == "一只橘猫"

    def test_none_content_is_empty(self):
        assert parse_description(self._response(None)) == ""

    #: 各种畸形响应。**不能**写成 parametrize 里的内联表达式 ——
    #: 用海象赋值会在类体里把上面的 _Response 覆盖掉（实测踩过）。
    MALFORMED = (
        None,
        object(),
        _response_type("R", choices=[]),                      # 没有 choices
        _response_type("R", choices=[object()]),              # choice 没有 message
        _response_type("R", choices=[_message_none()]),       # message 是 None
    )

    @pytest.mark.parametrize("bad", MALFORMED)
    def test_malformed_never_leaks_a_raw_exception(self, bad):
        """M2 审查 Y1b 的教训：畸形结构必须变成 ApiError，
        裸 AttributeError/TypeError 会穿透上层按 AyuanError 写的容错。"""
        with pytest.raises(ApiError):
            parse_description(bad)


class TestVisionResult:
    def test_empty_detection(self):
        assert VisionResult(text="  ").is_empty
        assert not VisionResult(text="一只猫").is_empty


# ══════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════

class TestCameraSettings:
    def test_defaults(self):
        settings = CameraSettings()
        assert settings.device_index == 0
        assert settings.max_edge == 1024
        assert settings.warmup_frames > 0, "不丢预热帧的话第一张可能是一片黑"

    def test_from_env(self):
        settings = CameraSettings.from_env(
            {"AYUAN_CAMERA_DEVICE": "1", "AYUAN_CAMERA_MAX_EDGE": "640", "AYUAN_CAMERA_QUALITY": "70"}
        )
        assert settings.device_index == 1
        assert settings.max_edge == 640
        assert settings.jpeg_quality == 70

    def test_garbage_falls_back(self):
        assert CameraSettings.from_env({"AYUAN_CAMERA_MAX_EDGE": "很大"}).max_edge == 1024

    def test_config_carries_it(self):
        assert Config.load({"AYUAN_CAMERA_DEVICE": "2"}).camera.device_index == 2
