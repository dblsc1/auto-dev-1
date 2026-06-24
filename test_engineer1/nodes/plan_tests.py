"""test_engineer1/nodes/plan_tests.py — Codex 读代码，生成结构化测试计划。"""
from __future__ import annotations
import json
import re

from ..state import TEState
from ..plan_schema import validate_test_plan
from ..prompts import TESTER_PLANNER_LONGTERM, TESTER_PLANNER_TASK, render


def _extract_plan_json(plan_text: str) -> dict | None:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", plan_text, re.DOTALL | re.IGNORECASE)
    candidates = [*fenced]
    if not candidates:
        starts = [m.start() for m in re.finditer(r"\{", plan_text)]
        for start in starts:
            depth = 0
            in_string = False
            escape = False
            for idx, char in enumerate(plan_text[start:], start):
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(plan_text[start:idx + 1])
                        break
    for candidate in reversed(candidates):
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("test_cases"), list):
            return obj
    return None


async def plan_tests(state: TEState, cfg) -> TEState:
    from programmer1.engine.agent import is_usage_limit_result, run_agent
    from programmer1.devlog import session as devlog
    from .run_mcp_tests import _persist_log, _run_shared_contract_check

    standard = state.get("standard") or cfg.human_standard
    if "api_contract" in getattr(standard, "all_checks", set()):
        contract_ok, contract_log, contract_failures, repair_modules = _run_shared_contract_check(
            state.get("contract_workspace", getattr(cfg, "contract_workspace", "")),
            state.get("backend_workspace", getattr(cfg, "backend_workspace", "")),
            state.get("frontend_workspace", getattr(cfg, "frontend_workspace", "")),
            state.get("shared_contracts", {}),
            state.get("active_modules", ["backend", "frontend"]),
        )
        artifact = _persist_log(cfg, "contract_check.log", contract_log)
        devlog.log_test("api_contract", passed=contract_ok,
                        detail="; ".join(contract_failures[:2]))
        devlog.log_output("test_engineer/contract", contract_log)
        if not contract_ok:
            reason = "; ".join(contract_failures[:3]) or "契约检查失败"
            print(f"  [test_engineer/contract] ❌ {reason}")
            return {
                "passed": False,
                "test_plan_error": reason,
                "run_logs": contract_log,
                "summary": "❌ api_contract",
                "failures": contract_failures,
                "phase": "api_contract",
                "failure_kind": "contract",
                "requires_human": False,
                "artifacts": [artifact],
                "repair_modules": repair_modules,
            }
        print("  [test_engineer/contract] ✅")
        contract_checked = True
        contract_artifacts = [artifact]
    else:
        contract_checked = False
        contract_artifacts = []

    criteria = "\n".join(f"  - {c}" for c in state.get("acceptance_criteria", []))
    longterm, taskmsg = render(
        TESTER_PLANNER_LONGTERM, TESTER_PLANNER_TASK,
        acceptance_criteria=criteria or "(无)",
        backend_workspace=state.get("backend_workspace", ""),
        frontend_workspace=state.get("frontend_workspace", ""),
        contract_workspace=state.get("contract_workspace", getattr(cfg, "contract_workspace", "")),
        shared_contracts_json=json.dumps(state.get("shared_contracts", {}), ensure_ascii=False),
    )

    print(f"  [test_engineer/planner] 读代码 + 制定测试计划（Codex）…")
    res = await run_agent(
        prompt=taskmsg,
        smart_level=cfg.planner_level,
        system_prompt=longterm,
        cwd=state.get("backend_workspace", "/tmp"),
        readonly_dirs=[
            state.get("backend_workspace", ""),
            state.get("frontend_workspace", ""),
            state.get("contract_workspace", getattr(cfg, "contract_workspace", "")),
        ],
        allowed_tools=["Read"],
        trace_name="test_engineer/planner",
    )

    if not res.ok or is_usage_limit_result(res):
        kind = "额度/会话限制" if is_usage_limit_result(res) else "模型调用失败"
        reason = f"test planner {kind}: {res.error or res.text or '无详细错误'}"
        print(f"  [test_engineer/planner] ✋ {reason}")
        return {
            "passed": False,
            "test_plan_error": reason,
            "run_logs": reason,
            "summary": f"✋ {kind}",
            "failures": [reason],
            "phase": "test_plan",
            "failure_kind": "usage_limit" if is_usage_limit_result(res) else "provider",
            "requires_human": True,
            "artifacts": contract_artifacts,
        }

    plan_text = res.text or ""
    plan_obj: dict | None = None
    parse_error = ""
    try:
        candidate = _extract_plan_json(plan_text)
        if candidate is None:
            parse_error = "未找到 JSON 对象"
        else:
            cases = candidate.get("test_cases")
            if not isinstance(cases, list) or not cases:
                raise ValueError("test_cases 必须是非空数组")
            for index, case in enumerate(cases, 1):
                if not isinstance(case, dict) or not case.get("id") or not isinstance(case.get("steps"), list):
                    raise ValueError(f"test_cases[{index}] 缺少 id 或 steps")
            plan_errors = validate_test_plan(candidate)
            if plan_errors:
                raise ValueError("test plan schema failed: " + "; ".join(plan_errors[:6]))
            plan_obj = candidate
            plan_text = json.dumps(candidate, ensure_ascii=False)
    except Exception as exc:
        parse_error = f"测试计划 JSON 无效: {exc}"

    if plan_obj is None:
        reason = parse_error or "测试计划不可用"
        print(f"  [test_engineer/planner] ✋ {reason}")
        return {
            "passed": False,
            "test_plan_error": reason,
            "run_logs": plan_text or reason,
            "summary": "✋ 测试计划无效",
            "failures": [reason],
            "phase": "test_plan",
            "failure_kind": "invalid_plan",
            "requires_human": True,
            "artifacts": contract_artifacts,
        }

    tc_count = len(plan_obj["test_cases"])
    print(f"  [test_engineer/planner] ✓ 测试计划就绪（{tc_count} 个用例）")
    return {
        "test_plan": plan_text,
        "test_plan_error": "",
        "contract_checked": contract_checked,
        "contract_artifacts": contract_artifacts,
    }
