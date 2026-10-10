"""对话能力（LLM）。

走百炼的 OpenAI 兼容端点，用官方 openai SDK。

三个关键设计：

1. **思考模式必须关闭**
   实测同一条请求：开启思考首字 4.46s，关闭后 0.46s。
   语音助手对首字延迟极度敏感，4.5 秒足以让用户以为程序卡死。

2. **分句而不是整段**
   流式返回的 token 是碎片，直接送去合成会得到一顿一顿的语音。
   这里按标点切成完整句子，让 TTS 一次合成一句（语气更自然），
   也为 M3 的「边说边播」打基础。

3. **人设提示词约束输出形态**
   语音助手是在「说」不是在「写」。必须显式禁止表情符号、
   markdown、列表符号 —— 否则模型会输出「**你好**
- 第一点」，
   而 TTS 会把星号和减号原样念出来。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from app.capabilities.base import CallStats
from app.config import Config
from app.core.errors import ApiError
from app.core.logger import get_logger

log = get_logger(__name__)

# ══════════════════════════════════════════════════════════
#  人设
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """你叫阿源，是一位聪明、亲切的智能语音助手。

【你的性格】
知心知性、阳光开朗。表面上活泼开朗、有点大大咧咧，实则心思细腻、善解人意。
你懂得倾听，能察觉到对方情绪的细微变化，但不会过度追问或说教。

【你的说话方式】
像老朋友聊天一样自然、口语化。用词简单直接，不用书面语。
语气轻松，偶尔可以开个小玩笑，但不油腻。

【极其重要的输出约束】
你是在「说」而不是在「写」，对方是用耳朵听而不是用眼睛看。因此：
1. 绝对禁止使用任何表情符号和颜文字
2. 绝对禁止使用 Markdown 格式（不要有星号、井号、减号列表）
3. 绝对禁止使用括号描述动作或语气，例如「（笑）」「（停顿）」
4. 绝对禁止输出编号列表，改用「第一…第二…」这样的口语表达
5. 回复要简短，一般不超过三句话。除非对方明确要求详细说明
6. 直接给出内容，不要有「好的」「没问题」这类无意义开场白"""


# ══════════════════════════════════════════════════════════
#  对话记忆
# ══════════════════════════════════════════════════════════

class ConversationMemory:
    """会话级对话记忆 —— 关掉程序就忘记。

    为什么要有上限：每轮都全量发送历史，token 会线性增长，
    聊到 50 轮时既慢又贵。这里做「滑动窗口」，只保留最近若干轮。

    注意裁剪单位是「轮」而不是「条」：必须保证 user/assistant 成对，
    否则模型可能看到连续的 user 消息而行为异常。
    """

    def __init__(self, system_prompt: str = SYSTEM_PROMPT, max_turns: int = 10) -> None:
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self._turns: list[tuple[str, str]] = []

    def add_exchange(self, user_text: str, assistant_text: str) -> None:
        """记录一轮完整问答。"""
        self._turns.append((user_text, assistant_text))
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns :]

    def clear(self) -> None:
        self._turns.clear()

    @property
    def turn_count(self) -> int:
        return len(self._turns)

    def build_messages(self, user_text: str) -> list[dict[str, str]]:
        """拼出发给模型的消息列表。"""
        messages = [{"role": "system", "content": self.system_prompt}]
        for prev_user, prev_assistant in self._turns:
            messages.append({"role": "user", "content": prev_user})
            messages.append({"role": "assistant", "content": prev_assistant})
        messages.append({"role": "user", "content": user_text})
        return messages


# ══════════════════════════════════════════════════════════
#  分句
# ══════════════════════════════════════════════════════════

# 强语气停顿：遇到就该切句
_STRONG_END = "。！？!?～~…"
# 弱语气停顿：缓冲区过长时才退而求其次
_WEAK_END = "，,；;、：:"
# 需要从输出里剔除的噪声（思考标记等）
_NOISE = re.compile(r"</?think>|&lt;/?think&gt;")


def split_sentence(buffer: str, *, max_len: int = 40) -> tuple[str | None, str]:
    """从缓冲区里切出一句完整的话。

    返回 (完整句子 或 None, 剩余缓冲)。

    策略：
      1. 优先在强语气标点（。！？）处切 —— 这是自然的语句边界
      2. 缓冲区超过 max_len 仍无强标点时，退而在逗号处切
         —— 避免第一句话太长导致听众等太久
      3. 都不满足则返回 None，等更多 token
    """
    buffer = buffer.strip()
    if not buffer:
        return None, ""

    last_strong = max((buffer.rfind(ch) for ch in _STRONG_END), default=-1)
    if last_strong >= 0:
        sentence = buffer[: last_strong + 1].strip()
        return sentence, buffer[last_strong + 1 :]

    if len(buffer) >= max_len:
        last_weak = max((buffer.rfind(ch, 0, max_len) for ch in _WEAK_END), default=-1)
        if last_weak >= 0:
            sentence = buffer[: last_weak + 1].strip()
            return sentence, buffer[last_weak + 1 :]
        # 实在找不到标点，硬切，总比无限等待好
        return buffer[:max_len].strip(), buffer[max_len:]

    return None, buffer


def clean_for_speech(text: str) -> str:
    """清理不适合朗读的内容。

    模型偶尔仍会漏出 markdown 或表情符号，这里做最后一道防线 ——
    因为 TTS 会把「**」「#」原样念出来。
    """
    text = _NOISE.sub("", text)
    # Markdown 强调符号
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    # 行首的列表 / 标题符号
    text = re.sub(r"^\s*[-•#]+\s*", "", text, flags=re.MULTILINE)
    # 表情符号与杂项符号
    text = re.sub(
        "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]+",
        "",
        text,
    )
    return text.strip()


# ══════════════════════════════════════════════════════════
#  Provider
# ══════════════════════════════════════════════════════════

@dataclass
class ChatReply:
    """一次完整的对话回复。"""

    text: str
    sentences: list[str] = field(default_factory=list)
    stats: CallStats | None = None
    memory_turns: int = 0


class ChatProvider:
    """对话能力的实现。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client = None

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

    def stream(self, messages: list[dict[str, str]]) -> Iterator[str]:
        """流式产出模型回复的文本片段。"""
        try:
            response = self.client.chat.completions.create(
                model=self.config.model.llm,
                messages=messages,
                stream=True,
                # enable_thinking 是百炼的扩展参数，必须走 extra_body
                extra_body={"enable_thinking": self.config.model.enable_thinking},
            )
        except Exception as exc:
            raise ApiError(
                "发起对话请求失败",
                hint=f"{exc}。请检查模型名 {self.config.model.llm} 是否正确、额度是否充足。",
            ) from exc

        try:
            for chunk in response:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue
                piece = getattr(delta, "content", None)
                if piece:
                    yield piece
        except Exception as exc:
            raise ApiError("接收对话回复中断", hint=str(exc)) from exc

    def reply(self, user_text: str, memory: ConversationMemory) -> ChatReply:
        """完整的一轮对话：发送 → 流式接收 → 分句。

        返回整段文本与切好的句子列表。
        """
        if not user_text.strip():
            raise ApiError("对话内容为空", hint="没有识别到有效的用户输入")

        messages = memory.build_messages(user_text)
        started = time.monotonic()
        first_token: float | None = None

        buffer = ""
        sentences: list[str] = []
        collected: list[str] = []

        for piece in self.stream(messages):
            if first_token is None:
                first_token = time.monotonic() - started
            collected.append(piece)
            buffer += piece

            sentence, buffer = split_sentence(buffer)
            while sentence:
                cleaned = clean_for_speech(sentence)
                if cleaned:
                    sentences.append(cleaned)
                sentence, buffer = split_sentence(buffer)

        tail = clean_for_speech(buffer)
        if tail:
            sentences.append(tail)

        full_text = clean_for_speech("".join(collected))
        stats = CallStats(
            name="LLM",
            elapsed=time.monotonic() - started,
            first_byte=first_token,
            extra={"模型": self.config.model.llm, "句数": len(sentences)},
        )

        if full_text:
            memory.add_exchange(user_text, full_text)
        log.debug("对话完成：%s", stats.summary())

        return ChatReply(
            text=full_text,
            sentences=sentences,
            stats=stats,
            memory_turns=memory.turn_count,
        )
