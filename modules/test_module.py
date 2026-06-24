"""modules/test_module.py — 黑盒测试节点（stub）。不读源码，只按验收标准跑测试。"""

from __future__ import annotations

from ..state import GraphState
from ..schemas import TestResult


def test_module(state: GraphState) -> GraphState:
    criteria = [ac for rep in state.get("module_reports", []) for ac in rep.acceptance_for_test]
    print(f"\n[test] 按 {len(criteria)} 条验收标准跑测试 …")
    for c in criteria:
        print(f"    [test] 验收: {c}")
    result = TestResult(passed=True, logs="(stub) all green")
    print(f"[test] {'✅ 通过' if result.passed else '❌ 失败'}")
    return {"test_result": result}
