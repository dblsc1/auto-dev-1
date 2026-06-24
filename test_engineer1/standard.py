"""test_engineer1/standard.py — E2E 集成测试标准。

和 programmer1/integrated_tester1/standard.py 设计一致：
  human_set_standard  — 铁律，不可删
  planner_set_standard — planner 只能加

检查项：
  playwright  — Playwright 浏览器 E2E 测试
  api_contract — HTTP 接口契约验证（requests 直接打后端）
  smoke       — 服务能否正常启动（端口可连）
"""

from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_CHECKS: frozenset[str] = frozenset({
    "playwright", "api_contract", "smoke",
})


@dataclass(frozen=True)
class E2ETestStandard:
    """
    required: 铁律 — human 在 test_standards_cfg.py 里定义
    extra:    planner 可追加，不可删 required
    """
    required: frozenset[str] = field(default_factory=frozenset)
    extra:    frozenset[str] = field(default_factory=frozenset)

    # E2E 运行时参数
    backend_port:    int = 8000
    frontend_port:   int = 3000
    startup_timeout: int = 10    # 等待服务就绪的秒数
    test_timeout:    int = 120   # playwright 超时秒数

    @property
    def all_checks(self) -> frozenset[str]:
        return self.required | self.extra

    def planner_add(self, *tools: str) -> "E2ETestStandard":
        valid = frozenset(t for t in tools if t in KNOWN_CHECKS)
        unknown = frozenset(tools) - KNOWN_CHECKS
        if unknown:
            import warnings
            warnings.warn(f"E2ETestStandard: 未知检查项 {unknown}，已忽略")
        return E2ETestStandard(
            required=self.required,
            extra=self.extra | valid,
            backend_port=self.backend_port,
            frontend_port=self.frontend_port,
            startup_timeout=self.startup_timeout,
            test_timeout=self.test_timeout,
        )

    def merged(self, other: "E2ETestStandard") -> "E2ETestStandard":
        return E2ETestStandard(
            required=self.required | other.required,
            extra=self.extra | other.extra,
            backend_port=self.backend_port,
            frontend_port=self.frontend_port,
            startup_timeout=self.startup_timeout,
            test_timeout=self.test_timeout,
        )
