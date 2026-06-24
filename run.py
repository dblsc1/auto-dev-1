"""run.py — 单模块真跑入口（CLI 示例）。

programmer1 是通用单元，不绑定业务。这里演示如何直接跑一个任务。
实际项目从 growth-garden-builder/graph.py 驱动多模块流程。

用法：
  python -m programmer1.run "实现斐波那契函数"
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

from .modules.code_module import run_module, ensure_workspace
from .engine.agent import shutdown
from .config import ModuleConfig
from .schemas import Task


async def run_task(cfg: ModuleConfig, task: Task):
    cfg.workspace = ensure_workspace(cfg.workspace)
    print("═" * 64)
    print(f"模块：{cfg.name}  工作区：{cfg.workspace}")
    print(f"任务：{task.title}")
    print("═" * 64)
    result = await run_module(cfg, task)
    await shutdown()
    rep = result.get("report")
    if rep:
        flag = "✅" if rep.ok else "⚠"
        print(f"\n{flag} {rep.module}: {rep.summary} | 改动 {rep.changed_files}")
    return result


def main() -> None:
    desc = next((a for a in sys.argv[1:] if not a.startswith("-")), "实现一个加减法工具函数")
    cfg = ModuleConfig(
        name="demo",
        workspace=str(Path.home() / "gg-workspace" / "demo"),
        real=True,
    )
    task = Task(id="run-1", title=desc, description=desc, complexity="simple",
                owner="demo", files_allowed=["main.py"])
    asyncio.run(run_task(cfg, task))


if __name__ == "__main__":
    main()
