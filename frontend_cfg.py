"""frontend_cfg.py — growth-garden 前端模块配置。"""
from __future__ import annotations
from pathlib import Path

from programmer1.config import MODULE_PLANNER_LEVEL, MODULE_REVIEWER_LEVEL, ModuleConfig
from programmer1.modules.code_module import build_code_module
from backend_cfg import BACKEND_CONFIG
from contract_cfg import CONTRACT_WORKSPACE
from test_standards_cfg import FRONTEND_HUMAN_STANDARD, FRONTEND_PLANNER_STANDARD

FRONTEND_CONFIG = ModuleConfig(
    name="frontend",
    workspace=str(Path.home() / "gg-workspace" / "frontend"),
    implementation_language="javascript",
    real=True,
    planner_level=MODULE_PLANNER_LEVEL,
    reviewer_level=MODULE_REVIEWER_LEVEL,
    readonly_dirs=[BACKEND_CONFIG.workspace, CONTRACT_WORKSPACE],
    git_remote="https://github.com/dblsc1/auto-dev-1.git",
    git_branch="frontend",
    human_standard=FRONTEND_HUMAN_STANDARD,
    planner_standard=FRONTEND_PLANNER_STANDARD,
    extra_rules=(
        "你是前端工程师：写可直接在浏览器打开的文件（HTML/CSS/原生 JS），单文件优先；"
        "不碰后端/数据库代码。"
        f"【接口规则】规划和编码前必须先 Read 契约文件 {CONTRACT_WORKSPACE}/backend_api.json；"
        "本任务要调用的 HTTP path、method、request 字段只能以 published_api 为准；"
        "available_api 只表示页面旧代码仍可使用的后端现有接口，不可替代本任务 consumes。"
        "不得自行发明接口路径或字段名。"
        "若契约文件不存在或缺少所需接口，必须上报，不要猜测。"
        "若 api_contract 检查多次失败（reviewer retry），主动 Read 后端代码核对实际实现。"
        "验收以「浏览器能正常渲染/交互」为准。"
    ),
)

FRONTEND_APP = build_code_module(FRONTEND_CONFIG)
