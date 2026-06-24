"""lite_tester/state.py — TesterState：测试子图内部状态。"""

from __future__ import annotations

from typing import TypedDict


class TesterState(TypedDict, total=False):
    acceptance_criteria:  list[str]   # 来自 programmer1 各模块 report.acceptance_for_test
    backend_workspace:    str
    frontend_workspace:   str
    pytest_ok:            bool
    pytest_logs:          str
    playwright_ok:        bool
    playwright_logs:      str
    passed:               bool        # 最终结论
    summary:              str
