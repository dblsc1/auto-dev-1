"""schemas.py — 系统里流动的数据形状，纯数据类，和 LLM/框架无关。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    complexity: str = "medium"
    owner: str = "backend"
    acceptance_criteria: list[str] = field(default_factory=list)
    files_allowed: list[str] = field(default_factory=list)
    # planner 输出的函数级规格（给 worker 最小化歧义）
    function_specs: list[str] = field(default_factory=list)      # ["def foo(a: int) -> dict"]
    interface_contract: dict = field(default_factory=dict)        # {"method":"POST","path":"/x",...}
    data_models: list[str] = field(default_factory=list)          # ["class Req(BaseModel): a: float"]
    # planner 本次追加的测试工具（只能加，不能删铁律）
    test_additions: list[str] = field(default_factory=list)
    # 顺序执行标记：True = 等前序所有任务完成后再运行（同文件多 worker 顺序追加时使用）
    sequential: bool = False


@dataclass
class CodingResult:
    success: bool = False
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    summary: str = ""


@dataclass
class TestResult:
    passed: bool = False
    logs: str = ""
    failures: list[str] = field(default_factory=list)
    phase: str = ""
    failure_kind: str = ""
    requires_human: bool = False
    artifacts: list[str] = field(default_factory=list)
    repair_modules: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    approved: bool = False
    action: str = "retry_worker"   # approve | retry_worker | retry_planner | reject | escalate
    reason: str = ""
    violations: list[str] = field(default_factory=list)   # 违规清单（越权/编译失败等）
    suggestions: list[str] = field(default_factory=list)  # 建议 arbiter 开放/限制的权限


@dataclass
class ModuleReport:
    module: str
    ok: bool
    summary: str = ""
    changed_files: list[str] = field(default_factory=list)
    acceptance_for_test: list[str] = field(default_factory=list)
    note: str = ""
    violations: list[str] = field(default_factory=list)   # 透传自 ReviewResult
    suggestions: list[str] = field(default_factory=list)  # 透传自 ReviewResult
