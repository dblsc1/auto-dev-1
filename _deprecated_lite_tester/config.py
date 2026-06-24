"""lite_tester/config.py — TesterConfig：测试团队的旋钮面板。

类比 programmer1 的 ModuleConfig：改 config 换行为，不碰代码。
real=False → stub（打印验收标准、假通过）；real=True → 真跑 pytest/playwright。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TesterConfig:
    # —— 工作区 ——
    backend_workspace:  str = ""   # 后端代码目录（pytest 在这跑）
    frontend_workspace: str = ""   # 前端目录（playwright 在这跑）

    # —— 行为 ——
    real: bool = True              # False = stub（免费跑结构）
    timeout: int = 60              # 单次测试超时（秒）

    # —— 开关 ——
    run_pytest:      bool = True   # 跑 pytest（后端）
    run_playwright:  bool = False  # 跑 playwright（前端，Godot 后期开启）

    # —— 报告 ——
    log_path: str = ""             # 测试日志写入路径（空 = 不写文件）
