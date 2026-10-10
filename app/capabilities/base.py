"""L4 能力层的公共基础设施。

所有能力（ASR / LLM / TTS / VLM）遵循同一套约定：

    · 构造时接收 Config，不读全局状态
    · 对外暴露「业务语义」的方法（transcribe / stream / synthesize），
      而不是「HTTP 细节」
    · 失败统一抛 ApiError，并附带对非程序员可操作的 hint

为什么要有这一层：云端厂商随时可能更换（百炼 → 本地 Ollama / vLLM）。
上层编排只认接口，不认实现。换厂商只改 config 里的 base_url。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import requests

# WAV 编解码统一由 app/audio/wav.py 提供（那里处理了「流式 WAV 假 size」的坑），
# 这里重新导出，让能力层不必知道它住在哪一层
from app.audio.wav import pcm_to_wav, wav_duration  # noqa: F401
from app.config import Config
from app.core.errors import ApiError
from app.core.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")


# ══════════════════════════════════════════════════════════
#  调用统计
# ══════════════════════════════════════════════════════════

@dataclass
class CallStats:
    """一次云端调用的耗时与体积，用于回答「为什么这么慢」。"""

    name: str
    elapsed: float = 0.0
    first_byte: float | None = None
    request_bytes: int = 0
    response_bytes: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [f"{self.name} {self.elapsed:.2f}s"]
        if self.first_byte is not None:
            parts.append(f"首字 {self.first_byte:.2f}s")
        if self.response_bytes:
            parts.append(f"下行 {self.response_bytes / 1024:.0f}KB")
        for key, value in self.extra.items():
            parts.append(f"{key}={value}")
        return " · ".join(parts)


# ══════════════════════════════════════════════════════════
#  HTTP 与重试
# ══════════════════════════════════════════════════════════

_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _looks_like_network_error(exc: Exception) -> bool:
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def call_with_retry(
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int = 3,
    delay: float = 1.0,
    on_retry: Callable[[int, int, str], None] | None = None,
) -> T:
    """执行一次云端调用，对「可重试的失败」自动重试。

    只重试三类情况：
      · 网络抖动（连接错误 / 超时）
      · 服务端 5xx、限流 429
      · 明确的 408 请求超时

    其余情况（401 密钥错、400 参数错）立即抛出 —— 重试没有意义，
    只会让用户多等几秒。

    on_retry 在每次失败后、真正重试前调用，参数 (第几次, 总次数, 原因)。
    订阅方用它给用户反馈 —— 否则最坏情况（3 次 60s 超时 + 退避 = 183 秒）
    会全程静默，用户只会以为程序死了然后强杀。
    """
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        reason = ""
        try:
            return fn()
        except ApiError as exc:
            last_error = exc
            # 显式的 retryable 优先；没有它时退回看 HTTP 状态码。
            # 网络层异常没有状态码，只能靠 retryable 表达「值得再试一次」。
            if not (exc.retryable or exc.status_code in _RETRYABLE_STATUS):
                raise
            reason = exc.message
            log.warning("%s 第 %d/%d 次失败（%s），准备重试", what, attempt, attempts, reason)
        except Exception as exc:  # noqa: BLE001 — 需要按类型分流
            last_error = exc
            if not _looks_like_network_error(exc):
                raise
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("%s 第 %d/%d 次失败（网络），准备重试", what, attempt, attempts)

        if attempt < attempts:
            if on_retry is not None:
                try:
                    on_retry(attempt, attempts, reason)
                except Exception:  # 订阅方出错不该拖垮重试
                    pass
            time.sleep(delay * attempt)

    raise ApiError(
        f"{what} 连续 {attempts} 次失败",
        hint=f"最后一次错误：{last_error}。请检查网络连接后重试。",
    )


class HttpClient:
    """带统一错误翻译的 HTTP 客户端。

    存在的意义：把 requests 的原始异常翻译成「人能看懂、且知道该怎么办」
    的 ApiError。编排层不需要认识 requests。
    """

    def __init__(
        self,
        config: Config,
        *,
        on_retry: Callable[[int, int, str], None] | None = None,
    ) -> None:
        self.config = config
        # 重试回调：让上层能把「正在重试」告诉用户。没有它的话，
        # 最坏情况（3 次 60s 超时 + 退避）会静默挂起约 3 分钟。
        self.on_retry = on_retry
        self._session = requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.secrets.dashscope_api_key}",
            "Content-Type": "application/json",
        }

    def post_json(self, url: str, payload: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        timeout = timeout or self.config.model.timeout
        try:
            response = self._session.post(url, headers=self._headers, json=payload, timeout=timeout)
        except requests.Timeout as exc:
            # 同时给 status_code 与 retryable：前者供错误提示使用，
            # 后者是重试判定的唯一语义来源
            raise ApiError(
                "请求云端超时",
                hint=f"超过 {timeout} 秒未响应，可能是网络较慢",
                status_code=408,
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            # retryable=True：网络层异常没有 HTTP 状态码，必须显式标记，
            # 否则会被 call_with_retry 当成「不可重试」直接抛出
            raise ApiError(
                "无法连接云端服务",
                hint=f"请检查网络连接：{exc}",
                retryable=True,
            ) from exc

        return self._handle(response)

    def get_bytes(self, url: str, *, timeout: int | None = None) -> bytes:
        """下载二进制内容（例如 TTS 返回的音频）。"""
        timeout = timeout or self.config.model.timeout
        try:
            response = self._session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise ApiError("下载音频失败", hint=str(exc), retryable=True) from exc

        if response.status_code != 200:
            raise ApiError(
                f"下载音频失败（HTTP {response.status_code}）",
                hint="音频链接可能已过期，请重试",
                status_code=response.status_code,
            )
        return response.content

    def _handle(self, response: requests.Response) -> dict[str, Any]:
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError as exc:
                raise ApiError("云端返回了无法解析的内容", hint=response.text[:200]) from exc

        # 尽量从响应体里挖出云端给的具体原因
        detail = response.text[:300]
        try:
            body = response.json()
            detail = body.get("message") or body.get("error", {}).get("message") or detail
            code = body.get("code") or body.get("error", {}).get("code") or ""
        except Exception:
            code = ""

        hints = {
            401: "API Key 无效或已过期，请到百炼控制台确认 DASHSCOPE_API_KEY",
            403: "免费额度可能已耗尽，请到百炼控制台查看余额或开通付费",
            429: "请求过于频繁，稍后会自动重试",
            400: "请求参数有误，通常是模型名写错或音频格式不被接受",
        }
        raise ApiError(
            f"云端返回错误（HTTP {response.status_code}）",
            hint=f"{hints.get(response.status_code, '')} {detail}".strip(),
            status_code=response.status_code,
        )
