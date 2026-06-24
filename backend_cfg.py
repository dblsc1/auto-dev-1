"""backend_cfg.py — growth-garden 后端模块配置。"""
from __future__ import annotations
from pathlib import Path

from programmer1.config import MODULE_PLANNER_LEVEL, MODULE_REVIEWER_LEVEL, ModuleConfig
from programmer1.modules.code_module import build_code_module
from test_standards_cfg import BACKEND_HUMAN_STANDARD, BACKEND_PLANNER_STANDARD

BACKEND_CONFIG = ModuleConfig(
    name="backend",
    workspace=str(Path.home() / "gg-workspace" / "backend"),
    implementation_language="python",
    real=True,
    planner_level=MODULE_PLANNER_LEVEL,
    reviewer_level=MODULE_REVIEWER_LEVEL,
    git_remote="https://github.com/dblsc1/auto-dev-1.git",
    git_branch="backend",
    human_standard=BACKEND_HUMAN_STANDARD,
    planner_standard=BACKEND_PLANNER_STANDARD,
    extra_rules=(
        "你是后端工程师：写 Python（FastAPI 优先，标准库次之），实现纯函数/路由逻辑；"
        "不碰前端文件。暴露 REST 接口，在 main.py 启动服务，默认端口 8000。"
        "【严禁】创建 auth.py 或任何不在 files_allowed 列表里的文件，认证逻辑直接写在 main.py 里。"
    ),
)

BACKEND_APP = build_code_module(BACKEND_CONFIG)
