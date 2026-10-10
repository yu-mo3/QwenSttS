
"""L5 事件契约的测试（M4 修 KI-006）。

这里锁的不是「某个函数返回什么」，而是**两个编排器发出的事件形状**。
KI-006 的成因是：事件名与载荷各写一份，谁也没错，合起来就错 ——
界面统一订阅后，assistant 那条路径少一个 total 就是必现的 KeyError。

所以测试分两层：
  1. 契约表本身自洽（每个事件名都有条目、没有孤儿常量）
  2. 用 ast **静态扫描**两个编排器的源码，逐个 _emit(...) 比对契约

第 2 层是关键：它在「有人改了载荷却忘了改契约」时立刻失败，
而动态测试只能覆盖到它恰好跑到的那条路径。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.orchestrator import events as ev

ORCHESTRATOR_DIR = Path(ev.__file__).resolve().parent
PIPELINE = ORCHESTRATOR_DIR / "pipeline.py"
ASSISTANT = ORCHESTRATOR_DIR / "assistant.py"
SOURCES = (PIPELINE, ASSISTANT)


# ══════════════════════════════════════════════════════
#  源码扫描工具
# ══════════════════════════════════════════════════════

def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def declared_event_constants() -> dict[str, str]:
    """events.py 里声明的全部 EVT_* 常量 -> 事件名。"""
    tree = parse(Path(ev.__file__).resolve())
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.startswith("EVT_"):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found[target.id] = node.value.value
    return found


def _payload_keys_of(node: ast.Call, path: Path) -> set[str]:
    """从一次 _emit 调用里取出载荷的键集合。

    项目里有两种写法，都必须认：
        self._emit(EVT_X, index=1, total=2)      # 编排器内部，好读
        self._emit(EVT_X, {"index": 1})           # 标准 EventCallback 签名

    只认一种会让另一种静默逃过校验 —— 这正是本文件要防的那类漏洞。
    """
    rest = node.args[1:]
    if not rest:
        if any(kw.arg is None for kw in node.keywords):
            pytest.fail(f"{path.name}:{node.lineno} 用了 ** 展开，契约无法静态校验")
        return {kw.arg for kw in node.keywords if kw.arg}

    if len(rest) != 1 or not isinstance(rest[0], ast.Dict):
        pytest.fail(f"{path.name}:{node.lineno} 的载荷既不是关键字参数也不是字典字面量")

    keys: set[str] = set()
    for key in rest[0].keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            pytest.fail(f"{path.name}:{node.lineno} 的载荷键不是字符串字面量")
        keys.add(key.value)
    return keys


def emit_calls(path: Path) -> list[tuple[str, set[str], int]]:
    """取出源码里所有 self._emit(EVT_X, ...) 的 (常量名, 键集合, 行号)。"""
    calls: list[tuple[str, set[str], int]] = []
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "_emit":
            continue
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Name) or not first.id.startswith("EVT_"):
            continue
        calls.append((first.id, _payload_keys_of(node, path), node.lineno))
    return calls


ALL_EMIT_CALLS = [(p, call) for p in SOURCES for call in emit_calls(p)]


# ══════════════════════════════════════════════════════
#  契约表自洽
# ══════════════════════════════════════════════════════

class TestContractTable:
    def test_every_constant_has_a_contract(self):
        """新加一个 EVT_* 却忘了写契约 —— 这条会拦下来。"""
        declared = set(declared_event_constants())
        assert declared, "应当声明了事件常量"
        mapped = {name for name, value in declared_event_constants().items() if value in ev.PAYLOAD_CONTRACTS}
        assert declared == mapped, f"缺少契约的事件：{sorted(declared - mapped)}"

    def test_all_events_matches_contracts(self):
        assert ev.ALL_EVENTS == frozenset(ev.PAYLOAD_CONTRACTS)

    def test_required_and_optional_do_not_overlap(self):
        for name, contract in ev.PAYLOAD_CONTRACTS.items():
            overlap = contract.required & contract.optional
            assert not overlap, f"{name} 的 required 与 optional 重叠：{overlap}"

    def test_event_names_are_unique(self):
        """两个常量指向同一个事件名会让契约表静默吞掉一个。"""
        values = list(declared_event_constants().values())
        assert len(values) == len(set(values)), "事件名有重复"

    def test_no_dead_event_constants(self):
        """定义了却没人发的事件是死代码。

        KI-006 里就藏着一个：EVT_SPEECH_START 定义了整个 M3 都没发过一次，
        界面若订阅它，会永远等不到。
        """
        emitted = {name for _, (name, _, _) in ALL_EMIT_CALLS}
        declared = set(declared_event_constants())
        assert not (declared - emitted), f"从未被发出的事件：{sorted(declared - emitted)}"


# ══════════════════════════════════════════════════════
#  源码里的每一次 _emit 都要合规
# ══════════════════════════════════════════════════════

class TestEmitSitesMatchContract:
    def test_there_are_emit_sites(self):
        assert len(ALL_EMIT_CALLS) >= 20, "扫描没拿到预期的 _emit 调用，测试自身可能失效了"

    def test_no_emit_site_misses_a_required_key(self):
        problems = []
        for path, (const, keys, lineno) in ALL_EMIT_CALLS:
            event = getattr(ev, const)
            contract = ev.PAYLOAD_CONTRACTS[event]
            missing = contract.required - keys
            if missing:
                problems.append(f"{path.name}:{lineno} {const} 缺 {sorted(missing)}")
        assert not problems, "\n".join(problems)

    def test_no_emit_site_sends_undeclared_keys(self):
        problems = []
        for path, (const, keys, lineno) in ALL_EMIT_CALLS:
            event = getattr(ev, const)
            contract = ev.PAYLOAD_CONTRACTS[event]
            extra = keys - contract.allowed
            if extra:
                problems.append(f"{path.name}:{lineno} {const} 多了 {sorted(extra)}")
        assert not problems, "\n".join(problems)

    def test_tts_done_carries_total_in_both_orchestrators(self):
        """KI-006 的原点：pipeline 有 total，assistant 没有。

        界面靠 total 显示「第 i / N 句」，缺了就是必现的 KeyError。
        """
        for path in SOURCES:
            sites = [c for c in emit_calls(path) if c[0] == "EVT_TTS_DONE"]
            if not sites:
                continue      # 该编排器不发这个事件
            for _, keys, lineno in sites:
                assert "total" in keys, f"{path.name}:{lineno} 的 tts_done 少了 total"
        assert "total" in ev.PAYLOAD_CONTRACTS["tts_done"].required

    def test_no_local_event_constants_in_orchestrators(self):
        """事件常量不许再在编排器里就地定义（KI-006 的根因）。"""
        for path in SOURCES:
            for node in parse(path).body:
                if not isinstance(node, ast.Assign):
                    continue
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id.startswith("EVT_"):
                    pytest.fail(f"{path.name}:{node.lineno} 又就地定义了 {target.id}")

    def test_orchestrators_never_store_the_timing_total(self):
        """合计必须现算。

        M2 审查 B2 在 pipeline 里修过一次，assistant 里又原样犯了一次——
        存下来的合计既漏掉后写进去的分项，又会在求和时被再加一遍。
        用静态检查锁住，比指望人记得可靠。
        """
        for path in SOURCES:
            for node in ast.walk(parse(path)):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if not isinstance(target, ast.Subscript):
                        continue
                    if not isinstance(target.value, ast.Attribute):
                        continue
                
                    if target.value.attr == "timings" and _const_key(target) == "合计":
                        pytest.fail(f"{path.name}:{node.lineno} 又往 timings 里存合计了")


def _const_key(subscript: ast.Subscript) -> str | None:
    node = subscript.slice
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


# ══════════════════════════════════════════════════════
#  missing_keys / unexpected_keys
# ══════════════════════════════════════════════════════

class TestKeyHelpers:
    def test_missing_keys_reports_required_absent(self):
        assert ev.missing_keys("tts_done", {"index": 1}) == ["duration", "total"]

    def test_missing_keys_empty_when_complete(self):
        payload = {"index": 1, "total": 2, "duration": 1.0}
        assert ev.missing_keys("tts_done", payload) == []

    def test_optional_keys_are_not_required(self):
        assert ev.missing_keys("playback_done", {"index": 1}) == []

    def test_unknown_event_is_not_an_error(self):
        """契约不是白名单：发一个表外事件不该让订阅方收不到。"""
        assert ev.missing_keys("something_new", {}) == []
        assert ev.unexpected_keys("something_new", {"x": 1}) == []

    def test_unexpected_keys_reports_extras(self):
        assert ev.unexpected_keys("wake", {"oops": 1}) == ["oops"]


# ══════════════════════════════════════════════════════
#  TurnResult 的计时
# ══════════════════════════════════════════════════════

class TestTurnResultTiming:
    def test_empty_has_no_summary(self):
        assert ev.TurnResult().timing_summary() == ""

    def test_pipeline_style_keys(self):
        result = ev.TurnResult(timings={"识别": 1.0, "对话": 1.5, "合成": 5.0, "播放": 2.4})
        assert "合计 9.90s" in result.timing_summary()

    def test_assistant_style_keys(self):
        result = ev.TurnResult(timings={"识别": 1.0, "对话": 2.0, "播报": 3.0})
        summary = result.timing_summary()
        assert "合计 6.00s" in summary
        assert "合成" not in summary, "assistant 没有「合成」这一项，不该凭空出现"

    def test_legacy_stored_total_is_excluded(self):
        """老代码存进来的「合计」不能再被加一遍。"""
        result = ev.TurnResult(timings={"识别": 1.0, "合计": 99.0})
        summary = result.timing_summary()
        assert "合计 1.00s" in summary, summary
        assert "100" not in summary

    def test_timing_order_covers_both_orchestrators(self):
        """两个编排器写的键都必须出现在展示顺序里，否则会凭空消失。"""
        assert set(ev.TIMING_ORDER) >= {"识别", "对话", "合成", "播报", "播放"}
