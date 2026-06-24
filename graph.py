"""graph.py — 单模块示例图（演示 / viz 用）。

programmer1 是通用编码单元，不绑定具体业务配置。
推荐用法（从调用方传入 cfg）：
  from programmer1.modules.code_module import build_code_module, run_module
  from programmer1.config import ModuleConfig

  cfg = ModuleConfig(name="backend", workspace="~/my-ws/backend", ...)
  app = build_code_module(cfg)           # 接进更大的图
  await run_module(cfg, task)            # 或单独跑

跑这个文件（stub demo）：
  python -m programmer1.graph --viz
  python -m programmer1.graph "实现一个斐波那契函数"
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

from .modules.code_module import build_code_module, run_module
from .config import ModuleConfig
from .schemas import Task

_DEMO_CFG = ModuleConfig(
    name="demo",
    workspace=str(Path.home() / "gg-workspace" / "demo"),
    real=False,
)
app = build_code_module(_DEMO_CFG)


def main() -> None:
    if "--viz" in sys.argv:
        print(app.get_graph().draw_mermaid())
        return
    desc = next((a for a in sys.argv[1:] if not a.startswith("-")), "实现一个加减法工具函数")
    task = Task(id="demo-1", title=desc, description=desc, complexity="simple",
                owner="demo", files_allowed=["main.py"])
    asyncio.run(run_module(_DEMO_CFG, task))


if __name__ == "__main__":
    main()
