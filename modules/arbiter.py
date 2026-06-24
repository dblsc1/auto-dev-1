"""modules/arbiter.py — 通用裁决节点。

arbiter_review_test: 看 TestResult 拍板（merge / rollback / escalate）。
任务拆分（intake）属于业务层，由调用方（如 growth-garden-builder/arbiter.py）实现。
"""

from __future__ import annotations

from ..state import GraphState
from ..config import MAX_TEST_RETRIES
from ..devlog import session as devlog


def arbiter_review_test(state: GraphState) -> GraphState:
    """看测试报告拍板：通过→merge；失败→回退；反复失败→升级给人。"""
    failed_reports = [rep for rep in state.get("module_reports", []) if not rep.ok]
    if failed_reports:
        lines = []
        for rep in failed_reports:
            note = rep.note or rep.summary or "模块报告失败"
            lines.append(f"{rep.module}: {note}")
        reason = "模块 report ok=false，硬门禁阻止合并：\n" + "\n".join(lines)
        print(f"[arbiter] ✋ {reason}")
        devlog.finish(reason)
        devlog.save()
        return {"decision": "escalate", "result": reason}

    # 打印各模块违规汇总（供人查阅）
    for rep in state.get("module_reports", []):
        if rep.violations:
            print(f"[arbiter] ⚠ [{rep.module}] 违规记录:")
            for v in rep.violations:
                print(f"    - {v}")
        if rep.suggestions:
            print(f"[arbiter] 💡 [{rep.module}] 建议（请检查 files_allowed / extra_rules）:")
            for s in rep.suggestions:
                print(f"    - {s}")

    tr = state.get("test_result")
    if tr and getattr(tr, "requires_human", False):
        phase = getattr(tr, "phase", "test") or "test"
        detail = "; ".join(getattr(tr, "failures", [])[:3]) or getattr(tr, "logs", "")[:300]
        reason = f"{phase} 无法可靠执行，停止流程等待人工处理：{detail}"
        print(f"[arbiter] ✋ {reason}")
        devlog.log_test(phase, passed=False, detail=detail)
        devlog.finish(reason)
        devlog.save()
        return {"decision": "escalate", "result": reason, "stop_reason": reason}

    if tr and tr.passed:
        print("[arbiter] ✅ 测试通过 → 决定 merge")
        devlog.log_test(getattr(tr, "phase", "e2e") or "e2e", passed=True,
                        detail=getattr(tr, "logs", "")[:80])
        return {"decision": "merge", "result": "✅ 全部模块完成、测试通过，可合并。"}

    retries = state.get("test_retries", 0) + 1
    failures = getattr(tr, "failures", []) if tr else []
    devlog.log_test(getattr(tr, "phase", "e2e") if tr else "e2e",
                    passed=False, detail="; ".join(failures[:3]))
    if retries > MAX_TEST_RETRIES:
        print("[arbiter] 🙋 测试反复失败 → 升级给你（人在回路）")
        devlog.finish("测试反复失败，需要人工介入")
        devlog.save()
        return {"decision": "escalate", "test_retries": retries,
                "result": "🙋 测试反复不过，需要你介入。"}

    repair_modules = set(getattr(tr, "repair_modules", []) if tr else [])
    if repair_modules:
        batches = state.get("task_batches", [])
        first_affected = next(
            (index for index, batch in enumerate(batches)
             if any(getattr(task, "owner", "") in repair_modules for task in batch)),
            None,
        )
        if first_affected is not None:
            retry_batches = batches[first_affected:]
            print(f"[arbiter] 🔄 测试契约失败 → 从批次 {first_affected} 重做 "
                  f"{sorted(repair_modules)}（第 {retries} 次）")
            return {
                "decision": "rollback", "test_retries": retries,
                "task_batches": retry_batches, "batch_cursor": 0,
                "module_tasks": retry_batches[0], "cursor": 0, "module_reports": [],
            }

    print(f"[arbiter] 🔄 测试失败 → 回模块返工（第 {retries} 次）")
    return {"decision": "rollback", "test_retries": retries, "cursor": 0, "module_reports": []}
