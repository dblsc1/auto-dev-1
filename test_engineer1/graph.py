"""test_engineer1/graph.py — E2E 测试工程师子图工厂。

子图结构：
  plan_tests → run_mcp_tests → done

plan_tests:  先跑契约硬闸，再由 Codex（level 5）读源码，输出结构化测试计划 JSON
run_mcp_tests: Python 服务生命周期 + Python Playwright：起服务 → 跑浏览器 → 清理 → 报告

用法（从 graph.py 的 _tester_node 调用）：
    from test_engineer1.graph import build_test_engineer
    te = build_test_engineer(cfg)
    out = await te.ainvoke({
        "backend_workspace":  ...,
        "frontend_workspace": ...,
        "acceptance_criteria": [...],
        "standard": effective_standard,
    })
    passed  = out["passed"]
    summary = out["summary"]
"""

from __future__ import annotations
import functools
from pathlib import Path

from langgraph.graph import StateGraph, START, END

from .config import TestEngineerConfig
from .state  import TEState


# ── stub（real=False 时用）────────────────────────────────────────────────────
def _stub_plan(state: TEState) -> TEState:
    print("  [test_engineer/planner] (stub) 生成测试计划")
    return {"test_plan": '{"test_cases":[{"id":"stub","title":"stub","steps":[]}]}'}

def _stub_run(state: TEState) -> TEState:
    print("  [test_engineer/runner] (stub) 假通过")
    return {"passed": True, "run_logs": "(stub)", "summary": "✅ stub 通过"}


def _route_after_plan(state: TEState) -> str:
    return "done" if state.get("requires_human") or state.get("test_plan_error") else "run_mcp_tests"


# ── 工厂 ─────────────────────────────────────────────────────────────────────
def build_test_engineer(cfg: TestEngineerConfig | None = None):
    """返回编译好的 CompiledStateGraph。"""
    if cfg is None:
        cfg = TestEngineerConfig()

    if cfg.real:
        # real E2E 需要写日志/临时文件；stub/viz 构建不应触碰工作区。
        Path(cfg.test_workspace).mkdir(parents=True, exist_ok=True)
        from .nodes.plan_tests    import plan_tests
        from .nodes.run_mcp_tests import run_mcp_tests

        _plan = functools.partial(plan_tests,    cfg=cfg)
        _run  = functools.partial(run_mcp_tests, cfg=cfg)
    else:
        _plan, _run = _stub_plan, _stub_run

    g = StateGraph(TEState)
    g.add_node("plan_tests",    _plan)
    g.add_node("run_mcp_tests", _run)
    g.add_edge(START,           "plan_tests")
    g.add_conditional_edges("plan_tests", _route_after_plan,
                            {"run_mcp_tests": "run_mcp_tests", "done": END})
    g.add_edge("run_mcp_tests", END)
    return g.compile()
