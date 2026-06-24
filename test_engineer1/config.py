"""test_engineer1/config.py — E2E 测试工程师的旋钮面板。

从外部 test_standards_cfg.py 注入 human_standard / planner_standard。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from programmer1.config import TEST_PLANNER_LEVEL, TEST_WORKER_LEVEL


def _default_e2e_standard():
    from .standard import E2ETestStandard
    return E2ETestStandard(required=frozenset({"playwright"}))

def _empty_e2e_standard():
    from .standard import E2ETestStandard
    return E2ETestStandard()


@dataclass
class TestEngineerConfig:
    # ── 工作区 ──
    backend_workspace:  str = ""
    frontend_workspace: str = ""
    contract_workspace: str = str(Path.home() / "gg-workspace" / "contracts")
    test_workspace:     str = str(Path.home() / "gg-workspace" / "tester")

    # ── 行为 ──
    real: bool = True              # False = stub

    # ── 测试标准（从 test_standards_cfg.py 注入）──
    human_standard:   object = field(default_factory=_default_e2e_standard)
    planner_standard: object = field(default_factory=_empty_e2e_standard)

    # ── 模型路由 ──
    planner_level: int = TEST_PLANNER_LEVEL
    worker_level:  int = TEST_WORKER_LEVEL

    # ── 超时 ──
    startup_timeout: int = 10      # 等待服务就绪（秒）
    test_timeout:    int = 120     # playwright 运行超时（秒）

    # ── 日志 ──
    log_path: str = ""             # 日志目录或报告前缀（空=写入 test_workspace）
