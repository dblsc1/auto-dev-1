"""lite_tester/run.py — 单独跑测试团队（不接顶层图）。

跑法（从 growth-garden-builder/ 目录）：
  python -m lite_tester.run                              # stub
  GG_REAL=1 python -m lite_tester.run ~/gg-workspace/backend
"""

from __future__ import annotations

import os
import sys
import asyncio

from .config import TesterConfig
from .tester import run_tester


async def main():
    real = os.getenv("GG_REAL") == "1"
    be_ws = sys.argv[1] if len(sys.argv) > 1 else ""
    fe_ws = sys.argv[2] if len(sys.argv) > 2 else ""

    cfg = TesterConfig(
        backend_workspace=be_ws,
        frontend_workspace=fe_ws,
        real=real,
        run_pytest=bool(be_ws),
        run_playwright=False,   # Godot 时代再开
    )
    demo_criteria = [
        "POST /login 正确账号返回 ok=true 和 token",
        "错误账号返回 ok=false",
        "index.html 浏览器打开有登录表单",
    ]
    result = await run_tester(cfg, demo_criteria, be_ws, fe_ws)
    print(f"\n最终：passed={result.get('passed')} | {result.get('summary')}")


if __name__ == "__main__":
    asyncio.run(main())
