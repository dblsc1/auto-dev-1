"""lite_tester/tester.py — 测试团队子图工厂，类比 programmer1 的 build_code_module。

build_tester(cfg) → CompiledStateGraph
  接收 TesterState（含验收标准 + 工作区路径），跑 pytest/playwright，返回 passed+summary。

run_tester(cfg, criteria, backend_ws, frontend_ws) → TesterState
  一行调用入口，不用手动造子图。

设计原则：
  · cfg.real=False → stub（打印验收标准、假通过），免费跑结构
  · cfg.run_pytest / cfg.run_playwright 分别控制是否跑（Godot 还没 playwright 套件时关掉）
  · 和 programmer1 完全解耦：只靠 list[str] acceptance_criteria 通信，不 import programmer1
"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.graph import StateGraph, START, END
from programmer1.engine.agent import run_agent

from .config import TesterConfig
from .state import TesterState


# ─────────────────── stub 节点 ───────────────────────────────────────────────
def _stub_test(state: TesterState) -> TesterState:
    criteria = state.get("acceptance_criteria", [])
    print(f"\n[lite-tester] (stub) 按 {len(criteria)} 条验收标准验证 …")
    for c in criteria:
        print(f"    ✓ {c}")
    return {"pytest_ok": True, "pytest_logs": "(stub)", "playwright_ok": True,
            "playwright_logs": "(stub)", "passed": True, "summary": f"stub 通过 {len(criteria)} 条"}


# ─────────────────── 真实节点 ────────────────────────────────────────────────
def _build_real_nodes(cfg: TesterConfig):
    from .runners.pytest_runner import run_pytest
    from .runners.playwright_runner import run_playwright

    async def run_backend_tests(state: TesterState) -> TesterState:
        ws = state.get("backend_workspace") or cfg.backend_workspace
        if not cfg.run_pytest or not ws:
            return {"pytest_ok": True, "pytest_logs": "(跳过)"}
        print(f"[lite-tester] pytest → {ws}")
        ok, log = await run_pytest(ws, cfg.timeout)
        print(f"[lite-tester] pytest {'✅' if ok else '❌'}")
        return {"pytest_ok": ok, "pytest_logs": log[-2000:]}  # 截断避免过长

    async def run_frontend_tests(state: TesterState) -> TesterState:
        ws = state.get("frontend_workspace") or cfg.frontend_workspace
        if not cfg.run_playwright or not ws:
            return {"playwright_ok": True, "playwright_logs": "(跳过，playwright=False)"}
        print(f"[lite-tester] playwright → {ws}")
        ok, log = await run_playwright(ws, cfg.timeout)
        print(f"[lite-tester] playwright {'✅' if ok else '❌'}")
        return {"playwright_ok": ok, "playwright_logs": log[-2000:]}

    def summarize(state: TesterState) -> TesterState:
        be_ok = state.get("pytest_ok", True)
        fe_ok = state.get("playwright_ok", True)
        passed = be_ok and fe_ok
        parts = []
        if cfg.run_pytest:
            parts.append(f"pytest={'✅' if be_ok else '❌'}")
        if cfg.run_playwright:
            parts.append(f"playwright={'✅' if fe_ok else '❌'}")
        summary = "、".join(parts) or "（无测试项）"
        print(f"[lite-tester] 结论：{'✅ 通过' if passed else '❌ 失败'} — {summary}")

        if cfg.log_path:
            log = {"passed": passed, "pytest_logs": state.get("pytest_logs", ""),
                   "playwright_logs": state.get("playwright_logs", "")}
            Path(cfg.log_path).write_text(json.dumps(log, ensure_ascii=False, indent=2))

        return {"passed": passed, "summary": summary}

    return run_backend_tests, run_frontend_tests, summarize


# ─────────────────── 工厂 ────────────────────────────────────────────────────
def build_tester(cfg: TesterConfig):
    """返回编译好的测试子图（CompiledStateGraph）。"""
    if cfg.real:
        run_be, run_fe, summarize = _build_real_nodes(cfg)
    else:
        # stub: 单节点直接通过
        def _noop(state): return {}
        run_be = run_fe = _noop
        summarize = _stub_test

    g = StateGraph(TesterState)
    g.add_node("run_backend_tests",  run_be)
    g.add_node("run_frontend_tests", run_fe)
    g.add_node("summarize",          summarize)
    g.add_edge(START, "run_backend_tests")
    g.add_edge("run_backend_tests", "run_frontend_tests")
    g.add_edge("run_frontend_tests", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


async def run_tester(
    cfg: TesterConfig,
    acceptance_criteria: list[str],
    backend_workspace: str = "",
    frontend_workspace: str = "",
) -> TesterState:
    """一行调用入口。返回完整 TesterState（含 passed, summary, logs）。"""
    app = build_tester(cfg)
    return await app.ainvoke({
        "acceptance_criteria": acceptance_criteria,
        "backend_workspace":  backend_workspace or cfg.backend_workspace,
        "frontend_workspace": frontend_workspace or cfg.frontend_workspace,
    })
