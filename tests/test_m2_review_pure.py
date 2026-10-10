
"""M2 独立审查的回归测试。

每一条都对应审查报告里实际复现过的一个问题。全部用桩注入，
零网络、零硬件、零密钥 —— 随时可跑。

审查者原话：「B1 就是这样漏掉的」「今天就会红」。
所以这些测试存在的意义不是覆盖率，而是**锁住已经踩过的坑**。
"""

from __future__ import annotations

import pytest

from app.capabilities.asr import SpeechRecognizer
from app.capabilities.llm import ConversationMemory, clean_for_speech, split_sentence
from app.core.errors import ApiError
from app.orchestrator.pipeline import TurnResult


class TestTimingSummary:
    """B2：分项与合计必须自洽。

    原先 speak() 存的是键「合成播放」，而 timing_summary() 显示的是
    「合成」「播放」，于是打印出「对话 1.53s · 合计 8.94s」——
    7.41s 在分项里凭空消失，恰好把最慢的一环藏了起来。
    """

    def test_breakdown_sums_to_total(self):
        result = TurnResult(timings={"识别": 1.0, "对话": 1.5, "合成": 5.0, "播放": 2.4})
        summary = result.timing_summary()
        assert "9.90s" in summary, f"合计应为 9.90s，实际输出：{summary}"

    def test_every_item_is_displayed(self):
        timings = {"识别": 1.0, "对话": 2.0, "合成": 3.0, "播放": 4.0}
        summary = TurnResult(timings=timings).timing_summary()
        for key in timings:
            assert key in summary, f"{key} 未出现在摘要中：{summary}"

    def test_empty_timings(self):
        assert TurnResult().timing_summary() == ""


class TestAsrParseHardening:
    """Y1b：畸形响应不得漏出裸异常。

    这是 M1 修过的同一类坑在 M2 重现：message.get 与 "".join 原先在
    try 之外，遇到畸形结构会抛原生 AttributeError / TypeError ——
    它们不属于 AyuanError 家族，上层容错完全兜不住。
    """

    @pytest.mark.parametrize(
        "body",
        [
            {},
            [],
            {"output": None},
            {"output": {}},
            {"output": {"choices": []}},
            {"output": {"choices": [{}]}},
            {"output": {"choices": [{"message": "字符串不是 dict"}]}},
            {"output": {"choices": [{"message": None}]}},
            {"output": {"choices": [{"message": {"content": None}}]}},
            {"output": {"choices": [{"message": {"content": [{"text": None}]}}]}},
            {"output": {"choices": [{"message": {"content": [None]}}]}},
            {"output": {"choices": [{"message": {"content": [{"text": 123}]}}]}},
            {"output": {"choices": [{"message": {"annotations": "不是列表"}}]}},
            {"output": {"choices": [{"message": {"annotations": [None, "x"]}}]}},
        ],
    )
    def test_never_leaks_raw_exception(self, body):
        """要么正确解析，要么抛 ApiError —— 绝不允许其他异常类型。"""
        try:
            SpeechRecognizer._parse(body)
        except ApiError:
            pass  # 正确：已收敛成项目异常
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"漏出了裸异常 {type(exc).__name__}: {exc}")

    def test_valid_structure(self):
        text, emotion = SpeechRecognizer._parse({
            "output": {"choices": [{"message": {
                "content": [{"text": "你好"}],
                "annotations": [{"emotion": "happy", "language": "zh"}],
            }}]}
        })
        assert text == "你好"
        assert emotion == "happy"

    def test_null_text_becomes_empty(self):
        """云端显式返回 null 时不能让 join 炸掉。"""
        text, _ = SpeechRecognizer._parse({
            "output": {"choices": [{"message": {"content": [{"text": None}]}}]}
        })
        assert text == ""


class TestCleanForSpeechExtra:
    """Y3 / G8：清洗不能篡改正文。"""

    def test_keeps_snake_case(self):
        """下划线被吞会念出错误的变量名，而清洗结果还会写进会话记忆。"""
        assert clean_for_speech("snake_case_name") == "snake_case_name"
        assert "snake_case" in clean_for_speech("变量 snake_case_name 的值")

    def test_keeps_math_expression(self):
        assert clean_for_speech("a*b*c") == "a*b*c"

    def test_still_strips_real_emphasis(self):
        assert clean_for_speech("_斜体_") == "斜体"
        assert clean_for_speech("**粗体**") == "粗体"

    def test_strips_numbered_list(self):
        assert clean_for_speech("1. 第一点") == "第一点"
        assert clean_for_speech("3) 第三点") == "第三点"

    def test_strips_link_keeps_text(self):
        result = clean_for_speech("看这里 [文档](https://example.com)")
        assert "文档" in result
        assert "https" not in result

    def test_fenced_code_pair(self):
        BT = chr(96)
        fenced = "文字 " + BT * 3 + "code" + BT * 3 + " 结束"
        result = clean_for_speech(fenced)
        assert "code" not in result
        assert "文字" in result and "结束" in result, f"围栏后的内容被误删：{result!r}"


class TestSplitSentenceEnglish:
    """Y2：英文句点必须被认作强标点。"""

    def test_splits_at_period(self):
        sentence, rest = split_sentence("Hello there. Next one")
        assert sentence == "Hello there."
        assert rest == "Next one"

    def test_does_not_cut_mid_word(self):
        """无标点的长英文句子应退到词边界，而不是把单词劈开。"""
        text = "English without any terminator just keeps going and going forever"
        sentence, rest = split_sentence(text, max_len=40)
        assert sentence is not None
        # 切点必须落在空白处（或整句返回）
        assert sentence.endswith(" ") or (sentence + rest).replace(" ", "") == text.replace(" ", "")
        assert not sentence.endswith("kee")


class TestMemoryBudget:
    """Y8：只限轮数挡不住「某一轮突然写两千字」。"""

    def test_char_budget_is_enforced(self):
        memory = ConversationMemory(max_turns=50)
        for i in range(30):
            memory.add_exchange(f"第{i}轮问题", "回答" * 100)  # 每轮约 200 字
        messages = memory.build_messages("新问题", char_budget=2000)
        total = sum(len(m["content"]) for m in messages)
        assert total <= 2000, f"实际 {total} 字，超出预算"

    def test_budget_counts_system_prompt(self):
        """system prompt 与本次输入同样占 token，必须计入。"""
        memory = ConversationMemory(max_turns=50)
        for i in range(20):
            memory.add_exchange("问题", "回答" * 100)
        messages = memory.build_messages("新问题", char_budget=1000)
        total = sum(len(m["content"]) for m in messages)
        assert total <= 1000

    def test_keeps_pairs_after_trimming(self):
        """裁剪后 user/assistant 仍必须成对交替。"""
        memory = ConversationMemory(max_turns=20)
        for i in range(20):
            memory.add_exchange(f"u{i}", "a" * 200)
        messages = memory.build_messages("最新问题", char_budget=800)
        roles = [m["role"] for m in messages]
        assert roles[0] == "system"
        assert roles[-1] == "user"
        body = roles[1:-1]
        assert len(body) % 2 == 0, f"历史不是成对的：{roles}"
        assert body[0::2] == ["user"] * (len(body) // 2)
        assert body[1::2] == ["assistant"] * (len(body) // 2)

    def test_small_conversation_untouched(self):
        memory = ConversationMemory(max_turns=10)
        memory.add_exchange("你好", "你好呀")
        messages = memory.build_messages("在吗")
        assert len(messages) == 4  # system + 1 轮 + 本次
