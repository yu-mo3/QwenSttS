"""视觉理解能力（VLM）。

## 为什么走 OpenAI 兼容端点，而 ASR/TTS 走原生端点

因为 VLM 本来就是标准的 chat.completions —— 只是 message 的 content
里多一个 image_url。实测百炼的兼容端点对 `qwen3-vl-plus` 工作正常，
所以这里复用 openai SDK，与 `ChatProvider` 同构。

ASR/TTS 则相反：兼容端点根本没有 `/audio/speech` 与 `/audio/transcriptions`
（实测 404 / SSL 层报错），它们只能走原生端点。

## 图片不落盘

拍到的可能是用户的脸、家里、桌面。以 base64 data URI 直传，
全程只在内存里 —— 与音频同一条「内存优先」铁律。
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from app.capabilities.base import CallStats
from app.config import Config
from app.core.errors import ApiError
from app.core.logger import get_logger

log = get_logger(__name__)

#: 默认的观察指令。
#:
#: 刻意写成「说人话」的要求 —— 与 LLM 的人设提示词同一个理由：
#: 阿源是在「说」而不是在「写」，用户是用耳朵听的。
#: 不这么写，模型会输出「**图片内容**：- 一个人\n- 一张桌子」这种，
#: 而 TTS 会把星号和减号原样念出来。
DEFAULT_PROMPT = (
    "用一两句话直接说清画面里最主要的东西。"
    "对方是用耳朵听的，所以不要用 Markdown、不要用列表符号、不要加表情符号，"
    "像跟朋友描述你看到的东西那样自然地说。"
)

#: 识别的粒度。'auto' 由服务端决定；'high' 更细但更慢更贵。
DEFAULT_DETAIL = "auto"


@dataclass
class VisionResult:
    """一次视觉理解的结果。"""

    text: str
    stats: CallStats | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


# ══════════════════════════════════════════════════════════
#  纯函数
# ══════════════════════════════════════════════════════════

#: 图片魔数 -> MIME。按内容判断而不是让调用方传 ——
#: 声明与实际不符的 data URI 是在对服务端撒谎，而多数服务端只是
#: 碰巧会嗅探内容才没出错。认出不来按 JPEG 处理（调用方本来就是拍 JPEG）。
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def detect_mime(data: bytes) -> str:
    """按魔数判断图片类型。"""
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def to_data_uri(image: bytes) -> str:
    """图片字节 -> data URI。

    空图片直接拒绝：把它发出去只会换来一个莫名其妙的云端报错，
    而在本地拦下来能给出一句人话。
    """
    if not image:
        raise ApiError("没有图片可以识别", hint="拍照返回了空内容，请重试")
    encoded = base64.b64encode(image).decode("ascii")
    return f"data:{detect_mime(image)};base64,{encoded}"


def parse_description(response: object) -> str:
    """从 SDK 响应里取出描述文本。

    整个遍历都在 try 里 —— M2 审查 Y1b 的教训：只把「取 message」那一行
    包进 try，后面的 `get` 一旦遇到畸形结构就抛裸 AttributeError / TypeError
    穿透到用户，而上层按 AyuanError 写的容错兜不住。
    """
    try:
        choices = getattr(response, "choices", None)
        if not choices:
            raise ValueError("响应里没有 choices")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise ValueError("choice 里没有 message")

        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content.strip()
        # 有些实现会返回 content 分段列表
        if isinstance(content, list):
            parts = []
            for item in content:
                text = getattr(item, "text", None)
                if text is None and isinstance(item, dict):
                    text = item.get("text")
                if text:
                    parts.append(str(text))
            return "".join(parts).strip()
        return ""
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise ApiError(
            "无法解析视觉理解结果",
            hint=f"返回结构不符合预期：{str(response)[:200]}",
        ) from exc


# ══════════════════════════════════════════════════════════
#  Provider
# ══════════════════════════════════════════════════════════

class VisionProvider:
    """视觉理解能力的实现。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client = None

    @property
    def model(self) -> str:
        return self.config.model.vlm

    @property
    def client(self):
        """延迟创建客户端：没有密钥时不至于在 import 期就炸。"""
        if self._client is None:
            from openai import OpenAI

            if not self.config.secrets.has_dashscope:
                raise ApiError(
                    "缺少百炼 API Key",
                    hint="请在项目根目录的 .env 文件中设置 DASHSCOPE_API_KEY",
                )
            self._client = OpenAI(
                api_key=self.config.secrets.dashscope_api_key,
                base_url=self.config.model.compatible_url,
                timeout=self.config.model.timeout,
            )
        return self._client

    def describe(
        self,
        jpeg: bytes,
        prompt: str | None = None,
        *,
        detail: str = DEFAULT_DETAIL,
    ) -> VisionResult:
        """把一张图交给视觉模型描述。

        Args:
            jpeg: JPEG 字节（调用方负责压缩到合理尺寸，见 app/camera/capture.py）
            prompt: 观察指令，默认 DEFAULT_PROMPT
            detail: 识别粒度
        """
        data_uri = to_data_uri(jpeg)
        prompt = prompt or DEFAULT_PROMPT

        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_uri, "detail": detail}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                # 视觉模型同样要关思考：开了之后一段描述要等十几秒，
                # 而「看看这是什么」本来就是要求快速回答的场景
                extra_body={"enable_thinking": self.config.model.enable_thinking},
            )
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                "视觉理解请求失败",
                hint=f"{exc}。请检查模型名 {self.model} 是否正确、额度是否充足。",
            ) from exc

        elapsed = time.monotonic() - started
        text = parse_description(response)
        stats = CallStats(
            name="VLM",
            elapsed=elapsed,
            request_bytes=len(data_uri),
            extra={"模型": self.model, "图片": f"{len(jpeg) // 1024}KB", "字数": len(text)},
        )
        log.debug("视觉理解完成：%s", stats.summary())
        return VisionResult(text=text, stats=stats)
