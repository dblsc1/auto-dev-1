"""integrated_tester1/standard.py — programmer1 模块内部测试标准。

设计原则：
  human_set_standard  ← 人定铁律，required 集合，planner 永远不能删
  planner_set_standard ← planner 只能往 extra 里加，never 改 required

合并方式（由 code_module 在 planner 节点后执行）：
  effective = human_set_standard.merged(planner_set_standard).planner_add(*this_run_additions)

已知检查项（KNOWN_CHECKS）：
  compile      — py_compile / import 验证（Python 模块）
  pytest       — pytest 跑通（exit 0 或 5）
  dom_check    — HTML 静态 DOM 结构检查（前端模块用）
  api_contract — 验证代码里真正实现了 planner interface_contract 声明的路径和字段（硬闸）
  typecheck    — mypy / pyright（未来扩展）
  lint         — flake8 / eslint（未来扩展）
"""

from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_CHECKS: frozenset[str] = frozenset({
    "compile", "pytest", "dom_check", "api_contract", "typecheck", "lint",
})


@dataclass(frozen=True)
class InternalTestStandard:
    """
    required: 铁律 — human 在外部配置文件里定义，运行时永远不变
    extra:    追加项 — planner 可通过 planner_add() 增加，永远不能删 required
    """
    required: frozenset[str] = field(default_factory=frozenset)
    extra:    frozenset[str] = field(default_factory=frozenset)

    @property
    def all_checks(self) -> frozenset[str]:
        return self.required | self.extra

    def planner_add(self, *tools: str) -> "InternalTestStandard":
        """Planner 专用：只往 extra 里加，required 原封不动。"""
        valid = frozenset(t for t in tools if t in KNOWN_CHECKS)
        unknown = frozenset(tools) - KNOWN_CHECKS
        if unknown:
            import warnings
            warnings.warn(f"InternalTestStandard: 未知检查项 {unknown}，已忽略")
        return InternalTestStandard(required=self.required, extra=self.extra | valid)

    def merged(self, other: "InternalTestStandard") -> "InternalTestStandard":
        """合并两个标准（human + planner_set）。两边的 required 和 extra 都取并集。"""
        return InternalTestStandard(
            required=self.required | other.required,
            extra=self.extra | other.extra,
        )

    def to_prompt_note(self) -> str:
        """生成注入 reviewer EXTRA_RULES 的说明文字。"""
        if not self.all_checks:
            return "（本模块无内部测试要求）"
        lines = ["【内部测试标准（由系统自动执行，结果已附在 notes 里）】"]
        for c in sorted(self.required):
            lines.append(f"  ★ {c}（铁律，必须通过）")
        for c in sorted(self.extra):
            lines.append(f"  + {c}（planner 追加）")
        return "\n".join(lines)
