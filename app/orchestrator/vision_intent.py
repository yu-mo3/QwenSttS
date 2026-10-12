"""视觉意图：判断这一轮用户是不是在要求「看一眼」。

## 为什么从 ASR 文本匹配，而不是给 KWS 加一个关键词

「看看这是什么」是一句**对话内容**，不是唤醒词。KWS 只负责「把她叫醒」，
而「让她看一眼」是叫醒之后的指令 —— 把它塞进唤醒词，等于要求用户
每次看东西前先喊一遍唤醒词，而且唤醒词会变成两个。

更实际的理由：KWS 的关键词 token 写法要**穷举试出来**
（status.md §六.2 —— 「阿源」的写法试了 10 种才成），加一个词就要
真人复测一轮。文本匹配没有这个问题，而且天然覆盖多种说法。

## 误触发的代价是不对称的

- **误触发**（不该拍却拍了）：多花一次 VLM 调用，有点意外但不难受
- **漏触发**（该拍却不拍）：用户说「看看这是什么」，她开始聊天气 —— 更像坏了

所以偏宽松。但宽松不等于没有边界，两条规则都是被真实的误判逼出来的：

1. **动作词必须出现在句首附近**。「我觉得你看这个方案怎么样」里有「你看」，
   但那是在征求意见，不是在要求拍照 —— 而说话人真要看东西时，
   「看看」几乎总在最前面。
2. **「这是什么」后面跟「意思/时候/原因」时不是要看**。
   「这是什么意思」「这是什么时候的事」都含「这是什么」，
   最初按「整句够短就算」的规则全被判成要看 —— 短句根本挡不住它们。
"""

from __future__ import annotations

#: 明确的「看」动作
LOOK_VERBS: tuple[str, ...] = (
    "帮我看看",
    "帮我瞅瞅",
    "看看",
    "看一下",
    "看一眼",
    "瞧瞧",
    "瞅瞅",
)

#: ⚠️ 这里**故意没有「你看」**。它太常做话语标记用
#: （「你看，我说得对吧」「我觉得你看这个方案怎么样」），
#: 加进来就会把征求意见当成要求拍照。
#: 「你看看」依然能被识别 —— 它包含「看看」，位置判定一样过。

#: 视觉指代。「这个/那个」不在这里 —— 它们太常见，
#: 「我觉得这个问题很有意思」会被误判；它们只在**剥触发词**时用得上。
LOOK_OBJECTS: tuple[str, ...] = (
    "这是什么",
    "这是啥",
    "什么东西",
    "啥东西",
    "我手里",
    "我手上",
)

#: 剥触发词时额外要剥掉的指示代词（只在句首剥，见 strip_trigger）
_DEICTIC: tuple[str, ...] = ("这个", "那个", "一下")

#: 「这是什么」后面紧跟这些词时**不是**要看 —— 它们在问别的东西。
#: 这一条是实测逼出来的：短句规则挡不住「这是什么意思」（才 6 个字）。
NOT_VISION_SUFFIXES: tuple[str, ...] = (
    "意思",
    "时候",
    "原因",
    "情况",
    "道理",
    "问题",
    "逻辑",
    "关系",
    "概念",
    "区别",
)

#: 动作词允许出现的最靠后位置。说话人真要看东西时，「看看」几乎总在最前面。
#: 2 是实测定下来的：「我觉得你看这个方案怎么样」里「你看」正好落在第 3 位，
#: 收到 2 之后它就被挡在外面了，而「你看看」「你看一下」（位置 1）照常通过。
VERB_MAX_POSITION = 2

#: 句子两端要剥掉的标点（ASR 有时会带出来）
_TRIM = " \t\r\n，。！？!?、,.;；:："


def _clean(text: str) -> str:
    return (text or "").strip().strip(_TRIM)


def is_vision_request(text: str) -> bool:
    """这句话是在要求「看一眼」吗？纯函数，可直接单测。"""
    cleaned = _clean(text)
    if not cleaned:
        return False

    # 规则 1：明确的动作词，且出现在句首附近
    for verb in LOOK_VERBS:
        position = cleaned.find(verb)
        if 0 <= position <= VERB_MAX_POSITION:
            return True

    # 规则 2：视觉指代，且后面没跟「意思 / 时候 / 原因」这类词
    for obj in LOOK_OBJECTS:
        position = cleaned.find(obj)
        if position < 0:
            continue
        rest = cleaned[position + len(obj) :]
        if not rest.startswith(NOT_VISION_SUFFIXES):
            return True

    return False


#: 按长度倒序，保证「帮我看看」先于「看看」被匹配掉
_STRIP_PHRASES: tuple[str, ...] = tuple(
    sorted(LOOK_VERBS + LOOK_OBJECTS + _DEICTIC, key=len, reverse=True)
)


def strip_trigger(text: str) -> str:
    """去掉**句首**的触发词，把剩下的当作用户的补充说明。

    「看看这是什么，这是我新买的杯子」-> 「这是我新买的杯子」

    **只从开头剥**：早先的实现在整句里做替换，结果
    「看看这是什么，这是我新买的杯子」被删成「这，这是我新买的杯子」——
    中间那半句里也有「这是什么」，一样被吃掉了。
    触发词是句首的引导语，不是可以在句中出现任意次的记号。

    剩下的补充说明会一并发给视觉模型，所以「看看这个零件有没有裂纹」
    这种带具体问题的说法不需要额外机制就能工作。
    """
    cleaned = _clean(text)
    changed = True
    while changed:
        changed = False
        for phrase in _STRIP_PHRASES:
            if cleaned.startswith(phrase):
                cleaned = cleaned[len(phrase) :].lstrip(_TRIM)
                changed = True
                break
    return cleaned.strip(_TRIM)


def with_vision_context(description: str, user_note: str = "") -> str:
    """把视觉描述与用户的补充说明拼成一段给 LLM 的输入。

    为什么不让 LLM 自己去看图：视觉能力藏在 L4 的 VisionProvider 后面，
    对话走的是 LLM。让两者各司其职 —— 先把「看到了什么」变成文字，
    再交给对话模型组织成人话回答。这样换视觉模型、换对话模型互不影响。
    """
    description = (description or "").strip()
    note = (user_note or "").strip()
    head = f"（我刚刚看了一眼，画面里是：{description}）"
    return f"{head} {note}".strip() if note else head
