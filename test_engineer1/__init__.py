"""test_engineer1 — E2E 集成测试工程师（并列于 programmer1）。

子图结构：plan_tests(Codex) → run_mcp_tests(contract check + Python Playwright)

外部接入：
    from test_engineer1.graph import build_test_engineer
    from test_engineer1.config import TestEngineerConfig
"""
from .graph  import build_test_engineer
from .config import TestEngineerConfig

__all__ = ["build_test_engineer", "TestEngineerConfig"]
