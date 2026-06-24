"""test_standards_cfg.py — 所有测试标准的外部配置入口。

【改这里，不碰内部代码。】

结构：每个模块两个变量：
  XXX_HUMAN_STANDARD   — 铁律，planner 永远不能删，只有人能改
  XXX_PLANNER_STANDARD — planner 可追加，不影响铁律

合并规则（由框架自动执行）：
  effective = human.merged(planner).planner_add(*this_run_planner_additions)

# ════════════════════════════════════════════════════════
# 如何扩展测试项（以新增 typecheck 为例）：
#   1. 把 "typecheck" 加到 KNOWN_CHECKS（standard.py）
#   2. runner.py 里实现 _run_typecheck()
#   3. 这里的 HUMAN / PLANNER standard 里加上 "typecheck"
# ════════════════════════════════════════════════════════
"""

from __future__ import annotations

from programmer1.integrated_tester1.standard import InternalTestStandard
from test_engineer1.standard import E2ETestStandard

# ── Backend 内部测试标准 ─────────────────────────────────────────────────────
# 铁律：每次都必须跑 compile + pytest（planner 不可删）
BACKEND_HUMAN_STANDARD = InternalTestStandard(
    required=frozenset({"compile", "pytest", "api_contract"}),
)
# planner 追加区：默认空，planner 可以在此 run 追加（如 "typecheck"）
BACKEND_PLANNER_STANDARD = InternalTestStandard(
    required=frozenset(),
    extra=frozenset(),
)

# ── Frontend 内部测试标准 ────────────────────────────────────────────────────
# 铁律：compile + dom_check（捕获 inline-style/className 冲突等静态问题）
FRONTEND_HUMAN_STANDARD = InternalTestStandard(
    required=frozenset({"compile", "dom_check", "api_contract"}),
)
FRONTEND_PLANNER_STANDARD = InternalTestStandard(
    required=frozenset(),
    extra=frozenset(),
)

# ── E2E 集成测试标准（test_engineer1 使用）──────────────────────────────────
# 铁律：playwright + api_contract（契约文件、后端路由、前端 fetch 必须一致）
E2E_HUMAN_STANDARD = E2ETestStandard(
    required=frozenset({"playwright", "api_contract"}),
    backend_port=8000,
    frontend_port=3000,
    startup_timeout=10,
    test_timeout=120,
)
E2E_PLANNER_STANDARD = E2ETestStandard(
    required=frozenset(),
    extra=frozenset(),
)
