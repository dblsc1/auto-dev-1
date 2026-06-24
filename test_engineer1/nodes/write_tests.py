"""Legacy node: DeepSeek turns a plan into pytest-playwright code.

This node is retained for compatibility only. The current graph directly uses
``plan_tests -> run_mcp_tests`` and does not mount this node.
"""
from __future__ import annotations
import json
from pathlib import Path

from ..state import TEState
from ..prompts import TESTER_WORKER_LONGTERM, TESTER_WORKER_TASK, render


async def write_tests(state: TEState, cfg) -> TEState:
    from programmer1.engine.agent import run_agent

    ws = cfg.test_workspace
    Path(ws).mkdir(parents=True, exist_ok=True)

    plan_text = state.get("test_plan", "")
    # 从 plan JSON 里取服务地址
    backend_url  = "http://localhost:8000"
    frontend_url = "http://localhost:3000"
    try:
        plan_obj = json.loads(plan_text)
        backend_url  = plan_obj.get("backend_base_url", backend_url)
        frontend_url = plan_obj.get("frontend_url", frontend_url)
    except Exception:
        pass

    longterm, taskmsg = render(
        TESTER_WORKER_LONGTERM, TESTER_WORKER_TASK,
        test_workspace=ws,
        test_plan=plan_text[:3000],
        backend_base_url=backend_url,
        frontend_url=frontend_url,
    )

    print(f"  [test_engineer/writer] 写 Playwright 测试文件（DeepSeek）…")
    res = await run_agent(
        prompt=taskmsg,
        smart_level=cfg.worker_level,
        system_prompt=longterm,
        cwd=ws,
        allowed_tools=["Read", "Write", "Edit"],
        trace_name="test_engineer/writer",
    )

    # 收集写入的文件
    test_files = [str(p) for p in Path(ws).glob("test_*.py")]
    print(f"  [test_engineer/writer] ✓ 写入 {len(test_files)} 个测试文件: {test_files}")
    return {"test_files": test_files, "test_workspace": ws}
