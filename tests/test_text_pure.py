"""对话文本处理测试。

这几个函数直接决定「听到的话自不自然」：分句错了会一顿一顿，
清理漏了 TTS 会把星号原样念出来。
"""

from __future__ import annotations

import pytest

from app.capabilities.llm import clean_for_speech, split_sentence


class TestSplitSentence:
    def test_splits_at_strong_punctuation(self) -> None:
        sentence, rest = split_sentence("你好。今天天气不错")
        assert sentence == "你好。"
        assert rest == "今天天气不错"

    @pytest.mark.parametrize("mark", ["。", "！", "？", "～", "…"])
    def test_all_strong_marks(self, mark: str) -> None:
        sentence, _ = split_sentence(f"你好{mark}剩余内容")
        assert sentence == f"你好{mark}"

    def test_no_punctuation_returns_none(self) -> None:
        sentence, rest = split_sentence("还没说完")
        assert sentence is None
        assert rest == "还没说完"

    def test_long_buffer_falls_back_to_comma(self) -> None:
        """缓冲过长时退而在逗号处切 —— 免得第一句让听众等太久。"""
        text = "这是一句很长很长的话，" + "继续" * 30
        sentence, rest = split_sentence(text, max_len=20)
        assert sentence is not None
        assert sentence.endswith("，")
        assert rest

    def test_empty(self) -> None:
        assert split_sentence("") == (None, "")

    def test_last_strong_mark_wins(self) -> None:
        """一次收到多个句子时，切到最后一个句号。"""
        sentence, rest = split_sentence("第一句。第二句。第三句没完")
        assert sentence == "第一句。第二句。"
        assert rest == "第三句没完"


class TestCleanForSpeech:
    def test_strips_markdown_bold(self) -> None:
        """TTS 会把星号念出来。"""
        assert clean_for_speech("**你好**") == "你好"

    def test_strips_emoji(self) -> None:
        assert clean_for_speech("你好😊") == "你好"
        assert clean_for_speech("开心🎉🎉") == "开心"

    def test_strips_think_tags(self) -> None:
        assert clean_for_speech("</think>你好") == "你好"

    def test_strips_list_markers(self) -> None:
        result = clean_for_speech("- 第一点\n- 第二点")
        assert "-" not in result

    def test_keeps_plain_text(self) -> None:
        text = "今天天气不错，要不要出去走走？"
        assert clean_for_speech(text) == text
