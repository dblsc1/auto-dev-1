"""state.py — 两级 State：顶层编排全局可见；模块子图局部隔离。"""

from __future__ import annotations

from typing import TypedDict, Dict

from .schemas import Task, CodingResult, ReviewResult, TestResult, ModuleReport
from .contracts import ContractReport, ExecutionContract


class GraphState(TypedDict, total=False):
    requirement:    str
    dev_mode:       bool
    module_tasks:   list[Task]      # 当前 batch 的任务列表
    cursor:         int             # 当前 batch 内的任务游标
    module_reports: list[ModuleReport]
    test_result:    TestResult
    test_retries:   int
    decision:       str
    result:         str
    stop_reason:    str
    # ── 批次化字段 ──────────────────────────────────────────────────────────
    task_batches:         list           # list[list[Task]]，每个元素是一批任务
    batch_cursor:         int            # 当前执行到第几批（0-indexed）
    batch_logs:           list           # list[dict]，每批完成后追加一条记录
    max_batch_iterations: int            # 最多执行多少批，防止死循环（默认 10）
    # ── 模块间共享契约（provider API + consumer pacts，下游 planner/tester 读取）──
    shared_contracts: dict


class ModuleState(TypedDict, total=False):
    module:         str
    task:           Task
    subtasks:       list[Task]
    coding_results: list[CodingResult]
    review:         ReviewResult
    retries:        int
    report:         ModuleReport
    workspace:      str
    assignments:    Dict[str, str]
    # planner 产出的执行契约 + worker 后的机械检查报告
    execution_contract: ExecutionContract
    shared_contracts:    dict
    contract_report:     ContractReport
    terminal_error:      str
    plan_feedback:       str
    # 任务首次进入 planner 时保存。重试依据该基线计算本轮改动，绝不清理失败现场。
    workspace_baseline:  dict[str, str]
    workspace_baseline_contents: dict[str, str]
    # integrated_tester1 输出（workers 之后、reviewer 之前）
    internal_test_passed: bool
    internal_test_logs:   str
