"""growth-garden-builder/graph.py — 三层顶层编排图。

调用层级（三层）：
  Layer 1  本文件 build()               ← 总调度：arbiter + dispatch + 汇总
  Layer 2  programmer1 code_module 子图  ← 编码团队：backend / frontend（/ godot 预留）
           test_engineer1 子图           ← 测试团队：E2E / Playwright MCP / contract
  Layer 3  code_module 内部             ← planner → workers → reviewer（在 programmer1 里）

Godot 预留：dispatch_router 已支持 "godot" owner，只需注册 godot_app 即可扩展。

跑法（从 growth-garden-builder/ 目录）：
  python -m graph                          # stub 全景（免费）
  python -m graph "做一个登录功能"          # stub + 换需求
  GG_REAL=1 python -m graph "做登录"       # 真跑（烧额度）
  python -m graph --viz                    # 打印 mermaid
"""

from __future__ import annotations

import os

# ════════════════════════════════════════════════════════════════════════════
# ★ 可调旋钮（改这里即可，不用动下面）
# ════════════════════════════════════════════════════════════════════════════

DEV_MODE = os.getenv("GG_DEV_MODE", "1") != "0"  # True = planner 审批；False = 全自动

# ════════════════════════════════════════════════════════════════════════════

# load_dotenv 必须在一切 programmer1 导入之前 — registry.py 在模块级读 DEEPSEEK_API_KEY
from dotenv import load_dotenv
load_dotenv()

import sys
import asyncio
import json
import datetime
import re
from dataclasses import replace
from pathlib import Path

from langgraph.graph import StateGraph, START, END

# 业务层裁决（拆任务 + 结果裁决）
from arbiter      import arbiter_intake, arbiter_batch_review, arbiter_review_test, arbiter_commit_push
# 模块配置（growth-garden 业务专属，programmer1 不知道）
from backend_cfg  import BACKEND_CONFIG
from frontend_cfg import FRONTEND_CONFIG
# programmer1：纯粹的编码单元
from programmer1.modules.code_module import build_code_module, ensure_workspace
from programmer1.state   import GraphState
from programmer1.schemas import CodingResult, ModuleReport, ReviewResult, Task, TestResult
from programmer1.contracts import (
    ContractCheck,
    ContractFailure,
    ContractReport,
    ExecutionContract,
    execution_contract_to_dict,
)
from programmer1.config  import MAX_TEST_RETRIES     # noqa: F401（顶层图沿用）
from programmer1.devlog import session as devlog

# test_engineer1：E2E + Playwright（起服务 + 浏览器测试）
from test_engineer1 import build_test_engineer, TestEngineerConfig
from test_standards_cfg import E2E_HUMAN_STANDARD, E2E_PLANNER_STANDARD
from contract_cfg import (
    BACKEND_CONTRACT_FILE,
    CONSUMER_CONTRACT_DIR,
    CONTRACT_WORKSPACE,
    ensure_contract_workspace,
)


# ─────────────────── 胶水：把子图包成顶层 node ──────────────────────────────
def _module_node(name: str, app):
    """Layer 1 → Layer 2：GraphState cursor → ModuleState → report 透传回。"""
    async def _node(state: GraphState) -> GraphState:
        task = state["module_tasks"][state["cursor"]]
        out = await app.ainvoke({"module": name, "task": task,
                                 "retries": 0, "assignments": {},
                                 "shared_contracts": state.get("shared_contracts", {})})
        report: ModuleReport = out["report"]
        updates = {
            "module_reports": [*state.get("module_reports", []), report],
            "cursor": state["cursor"] + 1,
        }
        if name == "backend" and report.ok:
            contract_payload = _write_backend_contract(
                out.get("subtasks", []),
                report,
                out.get("execution_contract"),
            )
            shared = dict(state.get("shared_contracts", {}))
            shared[name] = contract_payload
            updates["shared_contracts"] = shared
        elif report.ok:
            consumer_payloads = _write_consumer_contracts(
                name,
                report,
                out.get("execution_contract"),
            )
            if consumer_payloads:
                shared = dict(state.get("shared_contracts", {}))
                consumers = dict(shared.get("consumers") or {})
                for payload in consumer_payloads:
                    provider = str(payload.get("provider_module") or "")
                    consumer = str(payload.get("consumer_module") or name)
                    if not provider:
                        continue
                    provider_consumers = dict(consumers.get(provider) or {})
                    provider_consumers[consumer] = payload
                    consumers[provider] = provider_consumers
                shared["consumers"] = consumers
                updates["shared_contracts"] = shared
        return updates
    return _node


def _discover_backend_routes(workspace: str) -> list[dict]:
    """Best-effort static route inventory for existing backend code."""
    root = Path(workspace)
    routes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    route_pat = re.compile(
        r'@\w+\.(get|post|put|patch|delete)\s*\(\s*["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    for path in root.glob("**/*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for method, route in route_pat.findall(text):
            key = (method.upper(), route)
            if key in seen:
                continue
            seen.add(key)
            routes.append({
                "id": f"existing-{method.lower()}-{route.strip('/').replace('/', '-') or 'root'}",
                "method": method.upper(),
                "path": route,
                "request": {},
                "response_200": {},
                "source": str(path.relative_to(root)),
            })
    return routes


def _merge_api_items(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for item in group or []:
            method = str(item.get("method") or "").upper()
            path = str(item.get("path") or "")
            if not method or not path:
                continue
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            normalized = dict(item)
            normalized["method"] = method
            normalized["path"] = path
            merged.append(normalized)
    return merged


def _write_backend_contract(subtasks: list, report: ModuleReport, execution_contract=None) -> dict:
    """Persist backend planner interface_contracts for frontend/tester."""
    published_api = []
    contract_dict = execution_contract_to_dict(execution_contract) if execution_contract else {}
    contract_public_api = contract_dict.get("public_api") or []
    if contract_public_api:
        published_api.extend(contract_public_api)
    else:
        for st in subtasks or []:
            contract = getattr(st, "interface_contract", {}) or {}
            if not contract:
                continue
            published_api.append({
                "task_id": getattr(st, "id", ""),
                "title": getattr(st, "title", ""),
                "method": contract.get("method", ""),
                "path": contract.get("path", ""),
                "request": contract.get("request", {}),
                "response_200": contract.get("response_200", {}),
                "raw": contract,
            })
    published_api = _merge_api_items(published_api)
    discovered_api = _discover_backend_routes(BACKEND_CONFIG.workspace)
    available_api = _merge_api_items(published_api, discovered_api)

    ensure_contract_workspace()
    path = Path(CONTRACT_WORKSPACE) / BACKEND_CONTRACT_FILE
    payload = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_module": "backend",
        "changed_files": report.changed_files,
        "published_api": published_api,
        "available_api": available_api,
        # Backward-compatible aliases during migration.
        "contracts": published_api,
        "public_api": published_api,
        "execution_contract": contract_dict,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [contracts] backend API 契约已写入 {path}")
    devlog.log_output("contracts/backend_api.json",
                      json.dumps(payload, ensure_ascii=False, indent=2),
                      detailed=False)
    return payload


def _normalize_consume_item(item: dict, consumer_module: str) -> dict:
    target = dict(item.get("target") or item)
    method = str(target.get("method") or "").upper()
    path = str(target.get("path") or "")
    provider = str(target.get("provider") or "")
    normalized = {
        "id": str(item.get("id") or f"{consumer_module}-{provider}-{method}-{path}"),
        "consumer": consumer_module,
        "provider": provider,
        "method": method,
        "path": path,
        "request": target.get("request") or {},
        "response_200": target.get("response_200") or {},
        "allow_only": bool(target.get("allow_only", True)),
    }
    return normalized


def _write_consumer_contracts(
    consumer_module: str,
    report: ModuleReport,
    execution_contract=None,
) -> list[dict]:
    """Persist consumer-driven pacts declared by a module's consumes section."""
    contract_dict = execution_contract_to_dict(execution_contract) if execution_contract else {}
    consumes = [
        _normalize_consume_item(item, consumer_module)
        for item in (contract_dict.get("consumes") or [])
    ]
    by_provider: dict[str, list[dict]] = {}
    for item in consumes:
        provider = str(item.get("provider") or "")
        if not provider or not item.get("method") or not item.get("path"):
            continue
        by_provider.setdefault(provider, []).append(item)
    if not by_provider:
        return []

    ensure_contract_workspace()
    contract_dir = Path(CONTRACT_WORKSPACE) / CONSUMER_CONTRACT_DIR
    contract_dir.mkdir(parents=True, exist_ok=True)
    payloads: list[dict] = []
    for provider, interactions in sorted(by_provider.items()):
        payload = {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "consumer_module": consumer_module,
            "provider_module": provider,
            "changed_files": report.changed_files,
            "interactions": interactions,
            "execution_contract": contract_dict,
        }
        path = contract_dir / f"{consumer_module}__{provider}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  [contracts] consumer pact 已写入 {path}")
        devlog.log_output(
            f"contracts/{CONSUMER_CONTRACT_DIR}/{consumer_module}__{provider}.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            detailed=False,
        )
        payloads.append(payload)
    return payloads


def _tester_node(te_app):
    """顶层 test 节点：只使用 test_engineer1 执行 E2E / contract 检查。"""
    async def _node(state: GraphState) -> GraphState:
        criteria = [ac for rep in state.get("module_reports", [])
                    for ac in rep.acceptance_for_test]
        te_out = await te_app.ainvoke({
            "acceptance_criteria": criteria,
            "backend_workspace":  BACKEND_CONFIG.workspace,
            "frontend_workspace": FRONTEND_CONFIG.workspace,
            "contract_workspace": CONTRACT_WORKSPACE,
            "shared_contracts": state.get("shared_contracts", {}),
            "active_modules": [report.module for report in state.get("module_reports", []) if report.ok],
            "standard": E2E_HUMAN_STANDARD.merged(E2E_PLANNER_STANDARD),
        })
        passed = te_out.get("passed", False)
        logs = te_out.get("run_logs") or te_out.get("summary", "")
        return {"test_result": TestResult(
            passed=passed,
            logs=logs,
            failures=list(te_out.get("failures", [])),
            phase=te_out.get("phase", ""),
            failure_kind=te_out.get("failure_kind", ""),
            requires_human=bool(te_out.get("requires_human", False)),
            artifacts=list(te_out.get("artifacts", [])),
            repair_modules=list(te_out.get("repair_modules", [])),
        )}
    return _node


# ─────────────────── 路由 ────────────────────────────────────────────────────
# 已接入的模块 owner。扩展时往这加一行 + 在 build() 里注册节点，dispatch 自动路由。
_KNOWN_MODULES = {"backend", "frontend"}   # 未来："godot", "infra", ...


def _dispatch_router(state: GraphState) -> str:
    tasks = state.get("module_tasks", [])
    cur   = state.get("cursor", 0)
    if cur >= len(tasks):
        return "batch_review"   # 当前批次跑完 → 批次裁决
    owner = tasks[cur].owner
    return owner if owner in _KNOWN_MODULES else "batch_review"


def _batch_review_router(state: GraphState) -> str:
    """batch_review 完成后：还有批次 → dispatch；全部完成 → test。"""
    if state.get("decision") == "escalate":
        return "done"
    batches    = state.get("task_batches", [[]])
    batch_cur  = state.get("batch_cursor", 0)
    if batch_cur < len(batches):
        return "dispatch"
    return "test"


def _intake_router(state: GraphState) -> str:
    return "done" if state.get("decision") == "escalate" else "dispatch"


# ─────────────────── 构建 ────────────────────────────────────────────────────
def build(real: bool = False, checkpointer=None):
    """real=False → stub 全景；real=True → 真跑（backend先→frontend只读挂载→tester测）。"""
    be_cfg = BACKEND_CONFIG if real else replace(BACKEND_CONFIG, real=False)
    fe_cfg = replace(FRONTEND_CONFIG, readonly_dirs=[BACKEND_CONFIG.workspace, CONTRACT_WORKSPACE])
    if not real:
        fe_cfg = replace(fe_cfg, real=False)
    be_cfg = replace(be_cfg, dev_mode=DEV_MODE)
    fe_cfg = replace(fe_cfg, dev_mode=DEV_MODE)
    be  = build_code_module(be_cfg)
    fe  = build_code_module(fe_cfg)
    te = build_test_engineer(TestEngineerConfig(
        backend_workspace=BACKEND_CONFIG.workspace,
        frontend_workspace=FRONTEND_CONFIG.workspace,
        contract_workspace=CONTRACT_WORKSPACE,
        real=real,
        human_standard=E2E_HUMAN_STANDARD,
        planner_standard=E2E_PLANNER_STANDARD,
    ))

    g = StateGraph(GraphState)
    g.add_node("intake",       arbiter_intake)
    g.add_node("dispatch",     lambda s: {})
    g.add_node("backend",      _module_node("backend", be))
    g.add_node("frontend",     _module_node("frontend", fe))
    # ← 未来 Godot：g.add_node("godot", _module_node("godot", godot_app))
    g.add_node("batch_review", arbiter_batch_review)
    g.add_node("test",         _tester_node(te))
    g.add_node("review",       arbiter_review_test)
    g.add_node("done",         lambda s: {})
    g.add_node("commit_push",  arbiter_commit_push)

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", _intake_router,
                            {"dispatch": "dispatch", "done": "done"})
    g.add_conditional_edges("dispatch", _dispatch_router,
                            {"backend": "backend", "frontend": "frontend",
                             "batch_review": "batch_review"})
    g.add_edge("backend",  "dispatch")
    g.add_edge("frontend", "dispatch")
    g.add_conditional_edges("batch_review", _batch_review_router,
                            {"dispatch": "dispatch", "test": "test", "done": "done"})
    g.add_edge("test",     "review")
    g.add_conditional_edges("review",
                            lambda s: (
                                "dispatch" if s.get("decision") == "rollback"
                                else "commit_push" if s.get("decision") == "merge"
                                else "done"
                            ),
                            {"dispatch": "dispatch", "commit_push": "commit_push", "done": "done"})
    g.add_edge("commit_push", "done")
    g.add_edge("done", END)
    return g.compile(checkpointer=checkpointer)


app = build(real=False)   # 默认 stub：langgraph dev / 免费全景


async def _run(real: bool, req: str) -> dict:
    """带 interrupt 处理的运行循环：支持 arbiter 澄清 和 planner dev_mode 审批。"""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.types import Command

    checkpointer = MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=[
        Task, CodingResult, ReviewResult, ModuleReport, TestResult,
        ExecutionContract, ContractCheck, ContractFailure, ContractReport,
    ]))
    instance = build(real=real, checkpointer=checkpointer)
    thread = {"configurable": {"thread_id": "main-1"}}

    async def _stream(cmd):
        async for chunk in instance.astream(cmd, thread, stream_mode="updates"):
            for node_name in chunk:
                print(f"\n[graph] ▶ {node_name}")

    await _stream({"requirement": req, "dev_mode": DEV_MODE})

    # 循环处理所有 interrupt（arbiter 澄清 + 每个模块 planner 审批）
    while True:
        snap = await instance.aget_state(thread)
        if not snap.next:
            break

        interrupt_val: dict = {}
        for t in (snap.tasks or []):
            if hasattr(t, "interrupts") and t.interrupts:
                interrupt_val = t.interrupts[0].value
                break

        if "question" in interrupt_val:
            # arbiter 需要澄清需求
            print(f"\n[arbiter] ⏸ 澄清：{interrupt_val['question']}")
            answer = input("你的回答: ").strip()
            await _stream(Command(resume=answer))

        elif "plan" in interrupt_val:
            # planner dev_mode 审批
            plan = interrupt_val["plan"]
            log_path = interrupt_val.get("log_path", "")
            print(f"\n{'═'*60}")
            print(f"⏸  [planner] 方案审批（完整日志：{log_path}）\n")
            for i, s in enumerate(plan, 1):
                print(f"  {i}. {s['id']} 「{s['title']}」")
                print(f"     文件：{s['files_allowed']}")
                print(f"     验收：{s['acceptance_criteria']}")
            contract = interrupt_val.get("execution_contract") or {}
            if contract:
                print("     执行契约："
                      f"public_api={len(contract.get('public_api', []))} "
                      f"internal={len(contract.get('internal', []))} "
                      f"consumes={len(contract.get('consumes', []))} "
                      f"constraints={len(contract.get('constraints', []))}")
            answer = input("\n批准此方案？[y=继续 / n=拒绝重拆]: ").strip().lower()
            if answer == "n":
                reason = input("拒绝原因（将提供给 planner，可留空）: ").strip()
                decision = {"decision": "reject", "reason": reason}
            else:
                decision = {"decision": "approve", "reason": ""}
            print(f"  → {decision}\n{'─'*60}")
            await _stream(Command(resume=decision))

        else:
            # 未知 interrupt，透传空字符串继续
            print(f"\n[graph] ⚠ 未知 interrupt，自动继续：{interrupt_val}")
            await _stream(Command(resume=""))

    snap = await instance.aget_state(thread)
    return dict(snap.values)


def main() -> int:
    if "--viz" in sys.argv:
        print(app.get_graph().draw_mermaid())
        return 0
    real = os.getenv("GG_REAL") == "1"
    req  = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if not req:
        print("用法: python -m graph \"你的需求描述\"")
        return 2
    if real:
        BACKEND_CONFIG.workspace  = ensure_workspace(BACKEND_CONFIG.workspace)
        FRONTEND_CONFIG.workspace = ensure_workspace(FRONTEND_CONFIG.workspace)
        ensure_contract_workspace()
    final = asyncio.run(_run(real=real, req=req))
    print("\n" + "═" * 64 + "\n" + final.get("result", "(无结论)"))
    if final.get("decision") == "escalate":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
