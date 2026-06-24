"""integrated_tester1 — programmer1 模块的内部测试执行器。

用法（在 code_module 的 internal_tester 节点里）：
    from .integrated_tester1.runner import run_checks
    from .integrated_tester1.standard import InternalTestStandard
"""
from .standard import InternalTestStandard
from .runner import run_checks

__all__ = ["InternalTestStandard", "run_checks"]
