"""test_engineer1/state.py — E2E 测试子图内部状态。"""
from __future__ import annotations
from typing import TypedDict, Any


class TEState(TypedDict, total=False):
    # ── 输入（由 graph.py 的 _tester_node 注入）──
    backend_workspace:  str
    frontend_workspace: str
    contract_workspace: str
    shared_contracts: dict
    active_modules: list[str]
    acceptance_criteria: list[str]
    standard: Any                   # E2ETestStandard（不含 TypedDict 序列化）

    # ── plan_tests 输出 ──
    test_plan: str                  # Codex 生成的测试计划（文本）
    test_plan_error: str
    test_workspace: str             # E2E 日志与兼容遗留测试文件目录
    contract_checked: bool
    contract_artifacts: list[str]

    # ── 遗留 write_tests/run_tests 兼容字段（当前图不使用）──
    test_files: list[str]

    # ── run_mcp_tests 输出 ──
    run_logs: str
    passed: bool
    failures: list[str]
    phase: str
    failure_kind: str
    requires_human: bool
    artifacts: list[str]
    repair_modules: list[str]

    # ── 汇总 ──
    summary: str
