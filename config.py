"""config.py — 全局常量 + ModuleConfig（每个代码模块的旋钮面板）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .integrated_tester1.standard import InternalTestStandard

# ── 路由参考表（不用改，改 registry.py 换引擎）──
def _build_complexity_map() -> dict[str, int]:
    """Map human label → level int, derived from registry so adding levels is automatic."""
    from .engine.registry import REGISTRY
    labels = ["trivial", "simple", "medium", "hard", "complex", "expert"]
    levels = sorted(REGISTRY.keys())           # [1,2,3,4,5,6]
    result: dict[str, int] = {}
    for label, level in zip(labels, levels):
        result[label] = level
    # backward-compat aliases (old 3-string scale)
    result.setdefault("simple",  result.get("simple",  2))
    result.setdefault("medium",  result.get("medium",  3))
    result.setdefault("complex", result.get("complex", 5))
    return result

COMPLEXITY_TO_SMART: dict[str, int] = _build_complexity_map()
MAX_MODULE_RETRIES = 2
MAX_TEST_RETRIES   = 2


def configured_smart_level(name: str, default: int) -> int:
    """Read a role smart_level from env, keeping fallback behavior in agent.py."""
    env_names = (
        f"GG_{name.upper()}_LEVEL",
        f"GG_MODEL_{name.upper()}_LEVEL",
    )
    for env_name in env_names:
        raw = os.environ.get(env_name)
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if 1 <= value <= 6 else default
    return default


ARBITER_PLANNER_LEVEL = configured_smart_level("ARBITER_PLANNER", 5)
MODULE_PLANNER_LEVEL = configured_smart_level("MODULE_PLANNER", 3)
MODULE_REVIEWER_LEVEL = configured_smart_level("MODULE_REVIEWER", 4)
TEST_PLANNER_LEVEL = configured_smart_level("TEST_PLANNER", 3)
TEST_WORKER_LEVEL = configured_smart_level("TEST_WORKER", 3)

# ── 默认提示词（导入自 engine/prompts，这里只做转发方便 config 引用）──
def _default(name: str) -> str:
    from .engine.prompts import PLANNER_LONGTERM, WORKER_LONGTERM, REVIEWER_LONGTERM
    return {"planner": PLANNER_LONGTERM, "worker": WORKER_LONGTERM,
            "reviewer": REVIEWER_LONGTERM}[name]

def _default_standard():
    from .integrated_tester1.standard import InternalTestStandard
    return InternalTestStandard(required=frozenset({"compile"}))

def _empty_standard():
    from .integrated_tester1.standard import InternalTestStandard
    return InternalTestStandard()


@dataclass
class ModuleConfig:
    # —— 身份 & 工作区 ——
    name: str
    workspace: str                                      # ★ agent 可写目录（必须在 repo 外）
    readonly_dirs: list[str] = field(default_factory=list)  # ★ 只读上下文（能看不能改）
    implementation_language: str = "python"            # python | javascript | generic

    # —— 行为 ——
    real: bool = True                                   # False = stub（免费跑结构）

    # —— 路由档位 ——
    planner_level:    int = MODULE_PLANNER_LEVEL        # 只传 smart_level；fallback 在 agent.py
    reviewer_level:   int = MODULE_REVIEWER_LEVEL       # 只传 smart_level；fallback 在 agent.py
    complexity_levels: dict[str, int] = field(
        default_factory=lambda: dict(COMPLEXITY_TO_SMART))

    # —— 测试标准（从 test_standards_cfg.py 注入，不在这里写死）——
    human_standard:   "InternalTestStandard" = field(default_factory=_default_standard)
    planner_standard: "InternalTestStandard" = field(default_factory=_empty_standard)

    # —— 门禁 ——
    max_retries:   int  = MAX_MODULE_RETRIES
    require_pytest: bool = False                        # 保留兼容，由 human_standard 接管

    # —— git 推送 ——
    git_remote: str = ""      # 非空时 arbiter_commit_push 会 push；空 = 只本地 commit
    git_branch: str = ""      # 空时用模块名（"backend" / "frontend"）

    # —— 人在回路 ——
    dev_mode: bool = False   # True：planner 拆完写日志 + 暂停等审批；False：全自动

    # —— 提示词（延迟求值，避免循环导入）——
    planner_prompt:  str = field(default_factory=lambda: _default("planner"))
    worker_prompt:   str = field(default_factory=lambda: _default("worker"))
    reviewer_prompt: str = field(default_factory=lambda: _default("reviewer"))
    extra_rules:     str = ""
