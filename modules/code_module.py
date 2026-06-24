"""modules/code_module.py — 通用代码模块子图：planner → workers → reviewer。

build_code_module(cfg) 返回一个 CompiledStateGraph（可直接 ainvoke 或接进更大的图）。
cfg.real=False 时全 stub，免费看结构；True 时接真 LLM。
"""

from __future__ import annotations

import re
import json
import subprocess
import hashlib
import difflib
from pathlib import Path

from langgraph.graph import StateGraph, START, END

from ..state import ModuleState
from ..schemas import Task, CodingResult, ReviewResult, ModuleReport
from ..config import ModuleConfig, COMPLEXITY_TO_SMART
from ..contracts import (
    build_contract_from_subtasks,
    execution_contract_from_dict,
    execution_contract_to_dict,
    validate_execution_contract,
)
from ..devlog import session as devlog


# ─────────────────── git 小工具 ───────────────────────────────────────────────
def _git(ws: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", ws, *args],
                           capture_output=True, text=True, timeout=30)
        return r.stdout
    except Exception:
        return ""


def _norm(p: str, module: str) -> str:
    """剥掉开头 './' 和多余的 '模块名/' 前缀（planner 常误带）。"""
    p = p.strip().strip('"').lstrip("./")
    pre = module + "/"
    while p.startswith(pre):
        p = p[len(pre):]
    return p


def _trackable_workspace_file(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    if any(part == ".git" or part == "__pycache__" for part in parts):
        return False
    if parts and parts[0] == "contracts":
        return False
    if any(part.startswith(".") for part in parts):
        return False
    if path.suffix == ".pyc":
        return False
    return path.is_file()


def _workspace_snapshot(workspace: str) -> dict[str, str]:
    root = Path(workspace)
    if not root.exists():
        return {}
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if not _trackable_workspace_file(path, root):
            continue
        rel = str(path.relative_to(root))
        try:
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snapshot


def _workspace_contents(workspace: str) -> dict[str, str]:
    root = Path(workspace)
    if not root.exists():
        return {}
    contents: dict[str, str] = {}
    for path in root.rglob("*"):
        if not _trackable_workspace_file(path, root):
            continue
        rel = str(path.relative_to(root))
        try:
            contents[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return contents


def _changed_files(
    workspace: str,
    module: str = "",
    baseline: dict[str, str] | None = None,
) -> list[str]:
    if baseline is not None:
        current = _workspace_snapshot(workspace)
        return [
            _norm(path, module) if module else path
            for path in sorted(set(current) | set(baseline))
            if current.get(path) != baseline.get(path)
        ]
    out = _git(workspace, "status", "--porcelain", "-uall")
    files = []
    for line in out.splitlines():
        if len(line) <= 3:
            continue
        f = line[3:].strip().strip('"')
        if "__pycache__" in f or f.endswith(".pyc"):
            continue
        if f.startswith("contracts/"):
            continue
        # 过滤 planner/系统写入的 dotfile（.plan_log.json 等），不算越权
        basename = f.split("/")[-1]
        if basename.startswith("."):
            continue
        files.append(_norm(f, module) if module else f)
    return files


def _allowed_files_for_review(
    subtasks: list[Task],
    execution_contract,
    module: str,
) -> set[str]:
    allowed = {_norm(f, module) for st in subtasks for f in st.files_allowed}
    if not execution_contract:
        return allowed
    for check in getattr(execution_contract, "constraints", []) or []:
        if getattr(check, "kind", None) != "file":
            continue
        target = getattr(check, "target", {}) or {}
        for file_name in target.get("allowed") or []:
            allowed.add(_norm(str(file_name), module))
    return allowed


def _staged_diff(workspace: str) -> str:
    _git(workspace, "add", "-A")
    return _git(workspace, "diff", "--cached")


def _baseline_diff(
    workspace: str,
    baseline_contents: dict[str, str] | None,
    changed_files: list[str],
) -> str:
    if not baseline_contents:
        return _staged_diff(workspace)
    root = Path(workspace)
    chunks: list[str] = []
    for rel in changed_files:
        path = root / rel
        before = baseline_contents.get(rel, "")
        try:
            after = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeDecodeError):
            after = ""
        chunks.extend(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        ))
    return "".join(chunks)


def _try_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _approval_decision(value) -> tuple[str, str]:
    """Accept legacy strings and structured CLI approval decisions."""
    if isinstance(value, dict):
        return (str(value.get("decision") or "approve").lower(),
                str(value.get("reason") or "").strip())
    return str(value or "approve").lower(), ""


def _extract_json_array(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _read_review_json(workspace: str) -> tuple[str, str, list[str], list[str]]:
    """读取 reviewer 写入的 .review_result.json，返回 (action, reason, violations, suggestions)。
    JSON 解析失败 → escalate（代码处理代码，格式故障直接上报，不猜）。
    """
    path = Path(workspace) / ".review_result.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        verdict = str(data.get("verdict", "")).lower().strip()
        if verdict == "retry":
            repair_target = str(data.get("repair_target", "")).lower().strip()
            verdict = "retry_planner" if repair_target == "planner" else "retry_worker"
        if verdict not in ("approve", "retry_worker", "retry_planner", "escalate", "reject"):
            raise ValueError(f"无效 verdict 值: {verdict!r}")
        return (verdict,
                str(data.get("reason", "(无理由)")),
                [str(v) for v in data.get("violations", [])],
                [str(s) for s in data.get("suggestions", [])])
    except Exception as e:
        print(f"  [reviewer] ❌ .review_result.json 解析失败（{e}），工作流上报人工")
        return "escalate", f"reviewer 输出格式故障: {e}", [], []


_IGNORED_REVIEW_NOISE_MARKERS = (
    "contracts/",
    "contract_report.json",
    "execution_contract.json",
    ".plan_log.json",
    ".plan_pending.json",
    ".review_result.json",
    ".reviewer_log.jsonl",
    "500 行",
    "500 lines",
    "超过 500",
    "exceed 500",
    "exceeds 500",
)


def _review_retry_is_only_ignored_noise(
    action: str,
    reason: str,
    violations: list[str],
) -> bool:
    """Guardrail: soft reviewer cannot retry on system metadata or invented limits."""
    if action not in ("retry_worker", "retry_planner", "reject"):
        return False
    items = violations or [reason]
    if not items:
        return False
    for item in items:
        text = item.lower()
        if not any(marker.lower() in text for marker in _IGNORED_REVIEW_NOISE_MARKERS):
            return False
    return True


def _append_reviewer_log(workspace: str, entry: dict) -> None:
    """实时把一次 reviewer 调用追加到 .reviewer_log.jsonl（每行一条 JSON）。"""
    log_path = Path(workspace) / ".reviewer_log.jsonl"
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _contract_report_to_dict(report) -> dict:
    return {
        "passed": bool(getattr(report, "passed", False)),
        "failures": [
            {
                "check_id": getattr(failure, "check_id", ""),
                "kind": getattr(failure, "kind", ""),
                "reason": getattr(failure, "reason", ""),
                "repair_target": getattr(failure, "repair_target", ""),
            }
            for failure in getattr(report, "failures", []) or []
        ],
        "warnings": list(getattr(report, "warnings", []) or []),
        "logs": getattr(report, "logs", "") or "",
    }


# ─────────────────── stub 节点（免费，跑结构用）─────────────────────────────
def _stub_planner(state: ModuleState) -> ModuleState:
    task = state["task"]
    print(f"  [{state['module']}/planner] (stub) 拆「{task.title}」")
    subtasks = [
        Task(id=f"{task.id}.{i}", title=f"{task.title}·{c}", complexity=task.complexity,
             owner=state["module"], acceptance_criteria=[f"{task.title} {c} 行为正确"],
             files_allowed=task.files_allowed)
        for i, c in ((1, "A"), (2, "B"))
    ]
    contract = build_contract_from_subtasks(state["module"], subtasks)
    return {"subtasks": subtasks, "execution_contract": contract}


def _stub_workers(state: ModuleState) -> ModuleState:
    results = []
    for st in state.get("subtasks", []):
        level = COMPLEXITY_TO_SMART.get(st.complexity, 3)
        print(f"    [{state['module']}/worker] (stub) 写「{st.title}」(level={level})")
        results.append(CodingResult(success=True, changed_files=[f"{st.id}.py"],
                                    summary=f"实现 {st.title}"))
    return {"coding_results": results, "retries": state.get("retries", 0) + 1}


def _stub_reviewer(state: ModuleState) -> ModuleState:
    print(f"  [{state['module']}/reviewer] (stub) 直接通过")
    return {"review": ReviewResult(approved=True, action="approve", reason="(stub)")}


# ─────────────────── make_report（共享基础版，stub 用）──────────────────────
def _make_report(state: ModuleState) -> ModuleState:
    r = state.get("review", ReviewResult())
    # stub 模式：用 coding_results 的假文件名（无真实 workspace）
    changed = list(dict.fromkeys(  # deduplicate while preserving order
        f for cr in state.get("coding_results", []) for f in cr.changed_files
    ))
    report = ModuleReport(
        module=state["module"], ok=r.approved,
        summary=f"{state['module']} 完成 {len(state.get('subtasks', []))} 个子任务",
        changed_files=changed,
        acceptance_for_test=[ac for st in state.get("subtasks", []) for ac in st.acceptance_criteria],
        note="" if r.approved else r.reason,
        violations=r.violations,
        suggestions=r.suggestions,
    )
    mod = state["module"]
    ok_tag = "✅" if report.ok else "❌"
    print(f"  [{mod}/report] {ok_tag} ok={report.ok} | 改动 {len(changed)} 文件: {changed}")
    if report.violations:
        print(f"  [{mod}/report] ⚠ 违规记录:")
        for v in report.violations:
            print(f"    - {v}")
    if report.suggestions:
        print(f"  [{mod}/report] 💡 arbiter 建议:")
        for s in report.suggestions:
            print(f"    - {s}")
    return {"report": report}


# ─────────────────── 真 LLM 节点（cfg 驱动）────────────────────────────────
def _build_real_nodes(cfg: ModuleConfig):
    from ..engine.agent import is_usage_limit_result, run_agent, run_parallel, Job
    from ..engine.prompts import render, PLANNER_TASK, WORKER_TASK, REVIEWER_TASK
    from ..integrated_tester1.runner import run_checks

    name = cfg.name
    from ..engine.registry import REGISTRY as _REG
    _COMPLEXITY_HINT = "  ".join(
        f"{lvl}={spec.provider}/{spec.model or 'default'}"
        for lvl, spec in sorted(_REG.items())
    )
    _LANGUAGE = getattr(cfg, "implementation_language", "generic")
    _FUNCTION_EXAMPLE = (
        "async function requestHealth() -> Promise<void>"
        if _LANGUAGE == "javascript"
        else "async def health_check() -> dict[str, bool]"
    )

    _PLANNER_JSON_CONTRACT = f"""

【输出格式·严格遵守】只输出一个 JSON 对象，不要任何解释/markdown 围栏。
格式：{{"tasks": [...], "execution_contract": {{...}}, "test_additions": [...]}}
tasks 每个元素：
  {{"id":"...","title":"...","complexity": <1-6整数，见下方对照表>,
   "sequential": false,
   "acceptance_criteria":[...],"files_allowed":["相对工作区根的路径"],
   "function_specs":["{_FUNCTION_EXAMPLE}"],
   "interface_contract":{{"method":"POST","path":"/x","request":{{}},"response_200":{{}}}},
   "data_models":["class Req(BaseModel): a: float"]}}
execution_contract 是本模块硬约束层，供 worker 完成后 run_contract 机械检查：
  {{
    "module": "{name}",
    "version": 1,
    "public_api": [
      {{"id":"api-id","method":"POST","path":"/x","request":{{"field":"str"}},"response_200":{{"id":"int"}}}}
    ],
    "internal": [
      {{"id":"fn-x","kind":"function","severity":"required","target":{{"signature":"{_FUNCTION_EXAMPLE}"}}}},
      {{"id":"schema-x","kind":"schema","severity":"required","target":{{"fields":{{"field":"str"}}}}}}
    ],
    "consumes": [],
    "constraints": [
      {{"id":"files-only","kind":"file","severity":"required","target":{{"allowed":["main.py"]}}}}
    ],
    "assumptions": []
  }}
execution_contract 不能只含 files-only：必须至少声明一个 public_api、internal 或 consumes 行为检查。
若模块是 frontend 且 state_brief 中有 shared_contracts.backend.published_api：
  - consumes 必须声明 provider="backend"，method/path 只能是 published_api 中的已发布接口；
  - available_api 是后端已有完整接口面，只用于判断页面旧代码是否越权，不可替代本任务 consumes；
  - 不得让浏览器请求 contracts/backend_api.json，那个文件只供规划和机械检查。
可用 kind：file / function / route / schema / dom / fetch / consumes。
target 字段必须使用以下规范名，禁止自造字段：
  file: {{"allowed":["index.html"],"forbidden":[]}}
  function: {{"signature":"{_FUNCTION_EXAMPLE}"}} 或 {{"name":"requestHealth"}}
  route: {{"method":"GET","path":"/health","handler":"health_check"}}
  schema: {{"fields":{{"ok":"bool"}}}}
  dom: {{"ids":["health-check-button","health-status"]}}，不要写 elements/required_elements。
  fetch: {{"path":"/health","fields":["ok"]}} 或 {{"forbidden_paths":["contracts/backend_api.json"]}}
  consumes: {{"provider":"backend","method":"GET","path":"/health","allow_only":true}}
本模块实现语言：{_LANGUAGE}。frontend 必须使用 JavaScript function 签名，禁止写 Python def。
complexity 对照（直接填数字）：{_COMPLEXITY_HINT}
sequential: false（默认，可并行）; true（等前序所有任务完成后再跑，同文件顺序追加时用）
test_additions：此次追加的测试工具（只加不删铁律），例如 ["dom_check"]，无则 []
files_allowed 用【相对工作区根】的路径；sequential=false 的任务之间 files_allowed 必须互不相交。"""

    _DIFF_MAX = 6000

    async def planner(state):
        task: Task = state["task"]
        base_files = task.files_allowed or [f"{task.id}.py"]
        pending_path = Path(cfg.workspace) / ".plan_pending.json"
        log_path = Path(cfg.workspace) / ".plan_log.json"
        # System metadata must exist before capturing the task baseline, otherwise
        # creating it would look like a worker's unauthorized file change.
        gi = Path(cfg.workspace) / ".gitignore"
        if not gi.exists():
            gi.write_text(".*\n__pycache__/\n*.pyc\n")
        workspace_baseline = dict(state.get("workspace_baseline") or _workspace_snapshot(cfg.workspace))
        workspace_baseline_contents = dict(
            state.get("workspace_baseline_contents")
            or _workspace_contents(cfg.workspace)
        )

        def _backend_api() -> list[dict]:
            payload = state.get("shared_contracts", {}).get("backend") or {}
            return list(
                payload.get("published_api")
                or payload.get("public_api")
                or payload.get("contracts")
                or []
            )

        def _subtasks_from_raw(raw_tasks: list[dict], run_test_additions: list[str]) -> list[Task]:
            subtasks_out: list[Task] = []
            for i, d in enumerate(raw_tasks, 1):
                raw_complexity = d.get("complexity", task.complexity)
                # 兼容新格式（整数 1-6）和旧格式（字符串 "simple"/"medium"/"complex"）
                complexity = str(raw_complexity)
                subtasks_out.append(Task(
                    id=d.get("id") or f"{task.id}.{i}",
                    title=d.get("title", f"{task.title} 子任务{i}"),
                    description=d.get("description", ""),
                    complexity=complexity,
                    sequential=bool(d.get("sequential", False)),
                    owner=name,
                    acceptance_criteria=d.get("acceptance_criteria", []),
                    files_allowed=d.get("files_allowed") or base_files,
                    function_specs=d.get("function_specs", []),
                    interface_contract=d.get("interface_contract", {}),
                    data_models=d.get("data_models", []),
                    test_additions=run_test_additions,
                ))
            return subtasks_out

        def _plan_log_data(subtasks_in: list[Task]) -> list[dict]:
            return [{
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "complexity": s.complexity,
                "sequential": s.sequential,
                "files_allowed": s.files_allowed,
                "acceptance_criteria": s.acceptance_criteria,
                "function_specs": s.function_specs,
                "interface_contract": s.interface_contract,
                "data_models": s.data_models,
                "test_additions": s.test_additions,
            } for s in subtasks_in]

        def _contract_from_plan(plan_obj: dict, subtasks_in: list[Task]):
            raw_contract = plan_obj.get("execution_contract") or {}
            if isinstance(raw_contract, dict) and raw_contract:
                contract = execution_contract_from_dict(raw_contract, module=name)
            else:
                contract = build_contract_from_subtasks(name, subtasks_in)
            if not contract.module:
                contract.module = name

            allowed_files = sorted({
                file_name for subtask in subtasks_in
                for file_name in (subtask.files_allowed or [])
            })
            if allowed_files and not any(
                check.kind == "file" and check.target.get("allowed")
                for check in contract.constraints
            ):
                from ..contracts import ContractCheck
                contract.constraints.append(ContractCheck(
                    id="files-allowed", kind="file",
                    target={"allowed": allowed_files},
                    rationale="worker may only modify planner-approved files",
                ))

            return contract

        def _write_contract(contract) -> Path:
            contracts_dir = Path(cfg.workspace) / "contracts"
            contracts_dir.mkdir(parents=True, exist_ok=True)
            path = contracts_dir / "execution_contract.json"
            path.write_text(
                json.dumps(execution_contract_to_dict(contract), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return path

        def _validate_plan_contract(contract):
            report = validate_execution_contract(
                contract,
                shared_contracts=state.get("shared_contracts", {}),
                implementation_language=getattr(cfg, "implementation_language", "generic"),
            )
            if not report.passed:
                report_path = Path(cfg.workspace) / "contracts" / "contract_report.json"
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(_contract_report_to_dict(report), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  [{name}/planner] ✋ execution_contract preflight failed")
                for failure in report.failures:
                    print(f"    - {failure.check_id} [{failure.kind}] "
                          f"repair_target={failure.repair_target}: {failure.reason}")
                devlog.log_output(f"{name}/planner contract preflight", report.logs,
                                  detailed=False)
            return report

        review = state.get("review")
        review_feedback = (
            review.reason
            if review and review.action == "retry_planner"
            else ""
        )
        rejection_feedback = str(review_feedback or state.get("plan_feedback") or "").strip()
        if cfg.dev_mode and pending_path.exists():
            try:
                pending = json.loads(pending_path.read_text(encoding="utf-8"))
                if pending.get("module") != name or pending.get("task_id") != task.id:
                    raise ValueError(
                        f"pending plan belongs to {pending.get('module')}/{pending.get('task_id')}, "
                        f"current task is {name}/{task.id}"
                    )
                subtasks = _subtasks_from_raw(
                    pending.get("tasks", []),
                    pending.get("test_additions", []),
                )
                execution_contract = _contract_from_plan(pending, subtasks)
            except Exception as e:
                print(f"  [{name}/planner] ⚠ pending plan 读取失败，将重新规划：{e}")
                pending_path.unlink(missing_ok=True)
                subtasks = []
            if subtasks:
                preflight = _validate_plan_contract(execution_contract)
                if not preflight.passed:
                    pending_path.unlink(missing_ok=True)
                    rejection_feedback = "execution_contract preflight failed:\n" + preflight.logs
                    devlog.log_event(f"{name}/planner pending contract invalid; replanning")
                    return await planner({
                        **state,
                        "plan_feedback": rejection_feedback,
                        "workspace_baseline": workspace_baseline,
                        "workspace_baseline_contents": workspace_baseline_contents,
                    })
                log_data = _plan_log_data(subtasks)
                print(f"  [{name}/planner] ⏸ dev_mode: 读取待审批方案 {pending_path}")
                from langgraph.types import interrupt
                decision_value = interrupt({
                    "plan": log_data,
                    "log_path": str(log_path),
                    "execution_contract": execution_contract_to_dict(execution_contract),
                })
                decision, rejection_reason = _approval_decision(decision_value)
                if decision == "reject":
                    print(f"  [{name}/planner] ✗ 方案被拒绝，将重新拆分")
                    rejection_feedback = rejection_reason or "人工拒绝当前方案，未提供补充原因"
                    devlog.log_event(f"{name}/planner plan rejected; replanning: {rejection_feedback}")
                    pending_path.unlink(missing_ok=True)
                else:
                    print(f"  [{name}/planner] ✓ 方案已批准，继续执行")
                    devlog.log_event(f"{name}/planner plan approved")
                    devlog.log_output(f"{name}/planner approved plan",
                                      json.dumps(log_data, ensure_ascii=False, indent=2))
                    pending_path.unlink(missing_ok=True)
                    contract_path = _write_contract(execution_contract)
                    print(f"  [{name}/planner] execution_contract → {contract_path}")
                    return {
                        "subtasks": subtasks,
                        "execution_contract": execution_contract,
                        "workspace_baseline": workspace_baseline,
                        "workspace_baseline_contents": workspace_baseline_contents,
                    }

        devlog.log_event(f"{name}/planner workspace baseline captured", detailed=True)
        print(f"  [{name}/planner] 拆「{task.title}」…")
        longterm, taskmsg = render(
            cfg.planner_prompt, PLANNER_TASK,
            module=name, EXTRA_RULES=cfg.extra_rules,
            task=f"{task.title}\n描述：{task.description}\n基准可改文件：{base_files}",
            state_brief=json.dumps({"shared_contracts": state.get("shared_contracts", {})}, ensure_ascii=False),
            notes=(
                "拆成 1~3 个函数级子任务，别过度拆分。"
                + (f" 上次方案被人工拒绝，原因：{rejection_feedback}" if rejection_feedback else "")
                + (" 前端的 consumes 只能引用 shared_contracts.backend.public_api；"
                   "若存在 published_api 则优先使用 published_api；"
                   "契约 JSON 只供规划/检查，禁止在浏览器运行时 fetch 该文件。"
                   if name == "frontend" else "")
            ),
        )
        res = await run_agent(prompt=taskmsg + _PLANNER_JSON_CONTRACT,
                              smart_level=cfg.planner_level, system_prompt=longterm,
                              trace_name=f"{name}/planner")

        if not res.ok or is_usage_limit_result(res):
            kind = "额度/会话限制" if is_usage_limit_result(res) else "模型调用失败"
            reason = f"planner {kind}: {res.error or res.text or '无详细错误'}"
            print(f"  [{name}/planner] ✋ {reason}")
            return {
                "review": ReviewResult(approved=False, action="escalate", reason=reason),
                "terminal_error": reason,
                "workspace_baseline": workspace_baseline,
                "workspace_baseline_contents": workspace_baseline_contents,
            }

        # 解析新格式 {"tasks": [...], "test_additions": [...]}
        # 兼容旧格式（纯数组）
        raw_text = res.text or ""
        plan_obj: dict = {}
        try:
            import re as _re
            m = _re.search(r"\{.*\}", raw_text, _re.DOTALL)
            if m:
                plan_obj = json.loads(m.group(0))
        except Exception:
            pass
        raw_tasks = plan_obj.get("tasks") or _extract_json_array(raw_text)
        run_test_additions: list[str] = plan_obj.get("test_additions") or []

        from ..engine.registry import resolve as _resolve_spec
        subtasks: list[Task] = _subtasks_from_raw(raw_tasks, run_test_additions)
        if not subtasks:
            reason = "planner 没有产出可解析的 tasks；停止以避免用猜测的硬约束驱动 worker"
            print(f"  [{name}/planner] ✋ {reason}")
            return {
                "review": ReviewResult(approved=False, action="escalate", reason=reason),
                "terminal_error": reason,
                "workspace_baseline": workspace_baseline,
                "workspace_baseline_contents": workspace_baseline_contents,
            }
        execution_contract = _contract_from_plan(plan_obj, subtasks)
        preflight = _validate_plan_contract(execution_contract)
        if not preflight.passed:
            rejection_feedback = "execution_contract preflight failed:\n" + preflight.logs
            if "execution_contract preflight failed" in str(state.get("plan_feedback") or ""):
                reason = "planner 连续产出无效 execution_contract，停止等待人工处理\n" + preflight.logs
                return {
                    "review": ReviewResult(approved=False, action="escalate", reason=reason),
                    "terminal_error": reason,
                "workspace_baseline": workspace_baseline,
                "workspace_baseline_contents": workspace_baseline_contents,
            }
            return await planner({
                **state,
                "plan_feedback": rejection_feedback,
                "workspace_baseline": workspace_baseline,
                "workspace_baseline_contents": workspace_baseline_contents,
            })
        for st in subtasks:
            level = cfg.complexity_levels.get(st.complexity) or _try_int(st.complexity) or 3
            spec  = _resolve_spec(level)
            seq_tag = " [顺序]" if st.sequential else ""
            print(f"    → {st.id} 「{st.title}」 complexity={st.complexity}→L{level}"
                  f"({spec.provider}/{spec.model or 'default'}){seq_tag}  files={st.files_allowed}")
        if run_test_additions:
            print(f"  [{name}/planner] 追加测试项: {run_test_additions}")

        if cfg.dev_mode:
            log_data = _plan_log_data(subtasks)
            plan_payload = {
                "module": name,
                "task_id": task.id,
                "task_title": task.title,
                "tasks": log_data,
                "execution_contract": execution_contract_to_dict(execution_contract),
                "test_additions": run_test_additions,
            }
            log_path.write_text(json.dumps(plan_payload, ensure_ascii=False, indent=2))
            pending_path.write_text(json.dumps(plan_payload, ensure_ascii=False, indent=2))
            print(f"\n  [{name}/planner] ⏸ dev_mode: 方案已写入 {log_path}")
            print(f"  [{name}/planner] ⏸ 等待审批…\n")
            from langgraph.types import interrupt
            decision_value = interrupt({
                "plan": log_data,
                "log_path": str(log_path),
                "execution_contract": execution_contract_to_dict(execution_contract),
            })
            decision, rejection_reason = _approval_decision(decision_value)
            if decision == "reject":
                print(f"  [{name}/planner] ✗ 方案被拒绝，将重新拆分")
                pending_path.unlink(missing_ok=True)
                rejection_feedback = rejection_reason or "人工拒绝当前方案，未提供补充原因"
                devlog.log_event(f"{name}/planner plan rejected: {rejection_feedback}")
                return await planner({
                    **state,
                    "plan_feedback": rejection_feedback,
                    "workspace_baseline": workspace_baseline,
                    "workspace_baseline_contents": workspace_baseline_contents,
                })
            pending_path.unlink(missing_ok=True)
            devlog.log_event(f"{name}/planner plan approved")
            devlog.log_output(f"{name}/planner approved plan",
                              json.dumps(log_data, ensure_ascii=False, indent=2))

        contract_path = _write_contract(execution_contract)
        print(f"  [{name}/planner] execution_contract → {contract_path}")

        return {
            "subtasks": subtasks,
            "execution_contract": execution_contract,
            "workspace_baseline": workspace_baseline,
            "workspace_baseline_contents": workspace_baseline_contents,
        }

    async def workers(state):
        import asyncio as _asyncio
        from ..engine.registry import resolve as _resolve
        assignments = dict(state.get("assignments", {}))
        review = state.get("review")
        feedback = (
            review.reason
            if review and review.action in ("retry", "retry_worker")
            else "（首次实现）"
        )
        subtasks = state.get("subtasks", [])

        def _make_job(st: Task) -> Job:
            level = cfg.complexity_levels.get(st.complexity) or _try_int(st.complexity) or 3
            spec  = _resolve(level)
            longterm, taskmsg = render(
                cfg.worker_prompt, WORKER_TASK,
                module=name, EXTRA_RULES=cfg.extra_rules,
                task=f"{st.title}\n{st.description}",
                function_specs="\n".join(st.function_specs) or "(无)",
                interface_contract=json.dumps(st.interface_contract, ensure_ascii=False) if st.interface_contract else "(无)",
                execution_contract=(
                    json.dumps(execution_contract_to_dict(state["execution_contract"]), ensure_ascii=False)
                    if state.get("execution_contract") else "(无)"
                ),
                data_models="\n".join(st.data_models) or "(无)",
                acceptance="；".join(st.acceptance_criteria) or "(无)",
                files_allowed="、".join(st.files_allowed), feedback=feedback,
            )
            return Job(prompt=taskmsg, smart_level=level, cwd=cfg.workspace,
                       session_id=assignments.get(st.id),
                       readonly_dirs=cfg.readonly_dirs,
                       allowed_tools=["Read", "Write", "Edit", "Bash"],
                       system_prompt=longterm)

        async def _run_one(idx: int, st: Task, job: Job) -> None:
            model_tag  = f"{_resolve(job.smart_level).provider}/{_resolve(job.smart_level).model or 'default'}"
            resume_tag = " 续会话" if job.session_id else ""
            print(f"    [{name}/worker#{idx+1}] 「{st.title}」"
                  f" L{job.smart_level}({model_tag})  files={st.files_allowed}{resume_tag}")
            ar = await run_agent(prompt=job.prompt, smart_level=job.smart_level,
                                 session_id=job.session_id, cwd=job.cwd,
                                 allowed_tools=job.allowed_tools, system_prompt=job.system_prompt,
                                 readonly_dirs=job.readonly_dirs,
                                 trace_name=f"{name}/worker#{idx+1}:{st.id}")
            agent_results[idx] = ar
            msg = (ar.error or ar.text or "")[:200]
            status_ch = "✓" if ar.ok else "✗"
            if "401" in msg or "Authentication Fails" in msg:
                print(f"    [{name}/worker#{idx+1}:{st.id}] {status_ch} ⚠ API 认证失败")
            else:
                print(f"    [{name}/worker#{idx+1}:{st.id}] {status_ch} → {msg}")
            if ar.session_id:
                assignments[st.id] = ar.session_id

        agent_results: list = [None] * len(subtasks)
        has_sequential = any(st.sequential for st in subtasks)

        if has_sequential:
            # 顺序执行：按 subtasks 顺序逐个跑，每个都等前一个完成
            print(f"  [{name}/workers] ▶ 顺序执行 {len(subtasks)} 个 worker …")
            for i, st in enumerate(subtasks):
                await _run_one(i, st, _make_job(st))
        else:
            # 并行执行（默认）
            print(f"  [{name}/workers] ▶ 并行启动 {len(subtasks)} 个 worker …")
            await _asyncio.gather(*[_run_one(i, st, _make_job(st))
                                    for i, st in enumerate(subtasks)])

        baseline = state.get("workspace_baseline")
        changed = _changed_files(cfg.workspace, name, baseline)
        print(f"  [{name}/workers] ◀ 全部完成 | 改动文件: {changed or '(空)'}")
        results: list[CodingResult] = []
        for st, ar in zip(subtasks, agent_results):
            allowed = {_norm(a, name) for a in st.files_allowed}
            mine = [f for f in changed if f in allowed]
            results.append(CodingResult(success=ar.ok and bool(mine), changed_files=mine,
                                        summary=(ar.text or ar.error or "")[:300]))
        limit_results = [ar for ar in agent_results if ar and is_usage_limit_result(ar)]
        if limit_results:
            reason = "worker 额度/会话限制，停止流程并等待人工处理"
            print(f"  [{name}/workers] ✋ {reason}")
            return {
                "coding_results": results,
                "assignments": assignments,
                "retries": state.get("retries", 0) + 1,
                "review": ReviewResult(approved=False, action="escalate", reason=reason),
                "terminal_error": reason,
            }
        return {"coding_results": results, "assignments": assignments,
                "retries": state.get("retries", 0) + 1}

    async def internal_tester(state):
        """workers 完成后、reviewer 之前：程序化跑 InternalTestStandard 的检查项。"""
        subtasks = state.get("subtasks", [])
        # 合并 human + planner_set + 本次 planner 追加
        run_additions = list({a for st in subtasks for a in st.test_additions})
        effective = cfg.human_standard.merged(cfg.planner_standard).planner_add(*run_additions)
        check_names = sorted(effective.all_checks)
        if state.get("execution_contract"):
            check_names.append("run_contract")
        print(f"  [{name}/internal_tester] 跑检查项: {check_names}")
        passed, logs, contract_report = await run_checks(
            cfg.workspace,
            effective,
            subtasks=subtasks,
            execution_contract=state.get("execution_contract"),
            shared_contracts=state.get("shared_contracts", {}),
            implementation_language=getattr(cfg, "implementation_language", "generic"),
            workspace_baseline=state.get("workspace_baseline"),
        )
        tag = "✅" if passed else "❌"
        print(f"  [{name}/internal_tester] {tag}")
        if contract_report is not None:
            contract_tag = "✅" if contract_report.passed else "❌"
            print(f"  [{name}/run_contract] {contract_tag} "
                  f"failures={len(contract_report.failures)} "
                  f"warnings={len(contract_report.warnings)}")
            for failure in contract_report.failures:
                print(f"    - {failure.check_id} [{failure.kind}] "
                      f"repair_target={failure.repair_target}: {failure.reason}")
            report_path = Path(cfg.workspace) / "contracts" / "contract_report.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(_contract_report_to_dict(contract_report),
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  [{name}/run_contract] report → {report_path}")
        devlog.log_event(f"{name}/internal_tester {'passed' if passed else 'failed'} checks={check_names}")
        devlog.log_output(f"{name}/internal_tester", logs)
        return {
            "internal_test_passed": passed,
            "internal_test_logs": logs,
            "contract_report": contract_report,
        }

    async def reviewer(state):
        subtasks = state.get("subtasks", [])
        allowed = _allowed_files_for_review(
            subtasks, state.get("execution_contract"), name)
        changed = _changed_files(cfg.workspace, name, state.get("workspace_baseline"))

        if not changed:
            reason = "机械闸：没有任何文件改动"
            print(f"  [{name}/reviewer] ✗ {reason}")
            return {"review": ReviewResult(approved=False, action="retry_worker", reason=reason)}
        out_of_scope = [f for f in changed if f not in allowed]
        if out_of_scope:
            reason = f"机械闸：改了越权文件 {out_of_scope}"
            viols = [f"越权文件 {f}：worker 未经授权创建/修改" for f in out_of_scope]
            suggs = [f"若业务需要 {f}，请 arbiter 将其加入对应 task 的 files_allowed；"
                     f"否则在 extra_rules 中明确禁止 worker 创建该文件" for f in out_of_scope]
            print(f"  [{name}/reviewer] ✗ {reason}")
            print(f"    允许文件: {sorted(allowed)}")
            print(f"    越权文件: {out_of_scope}")
            for st in subtasks:
                print(f"    subtask {st.id}: files_allowed={st.files_allowed}")
            print(f"  [{name}/reviewer] 💡 建议：{suggs[0] if suggs else ''}")
            return {"review": ReviewResult(approved=False, action="retry_worker", reason=reason,
                                           violations=viols, suggestions=suggs)}

        # ③ 内部测试结果（由 internal_tester 节点预先跑完）
        it_passed = state.get("internal_test_passed", True)
        it_logs   = state.get("internal_test_logs", "（未跑）")
        if not it_passed:
            contract_report = state.get("contract_report")
            planner_failure = bool(contract_report and any(
                failure.repair_target == "planner"
                for failure in contract_report.failures
            ))
            action = "retry_planner" if planner_failure else "retry_worker"
            reason = "机械闸：内部测试/执行契约未通过\n" + it_logs[-1600:]
            print(f"  [{name}/reviewer] ✋ {action} · {reason[:180]}")
            return {"review": ReviewResult(approved=False, action=action, reason=reason)}
        test_note = (
            f"③ 内部测试已由系统自动执行，结果如下：\n"
            f"{'✅ 通过' if it_passed else '❌ 失败'}\n{it_logs[-1000:]}\n"
            f"{'测试通过，无需重跑。' if it_passed else '测试失败，必须 retry。'}"
        )
        diff = _baseline_diff(
            cfg.workspace,
            state.get("workspace_baseline_contents"),
            changed,
        )[:_DIFF_MAX]
        # 把测试标准注入 reviewer prompt
        test_standard_note = cfg.human_standard.merged(cfg.planner_standard).to_prompt_note()
        longterm, taskmsg = render(
            cfg.reviewer_prompt, REVIEWER_TASK,
            module=name, EXTRA_RULES=cfg.extra_rules,
            TEST_STANDARD_NOTE=test_standard_note,
            task="；".join(f"{st.title}[验收:{','.join(st.acceptance_criteria)}]" for st in subtasks),
            changed_files="、".join(changed) or "(无改动)",
            diff=diff or "(空)", notes=test_note,
        )
        import datetime as _dt
        review_json_path = Path(cfg.workspace) / ".review_result.json"
        review_json_path.unlink(missing_ok=True)   # 每轮清空，防止读到上一轮残留

        retries_now = state.get("retries", 0)
        log_entry: dict = {
            "ts":      _dt.datetime.now().isoformat(timespec="seconds"),
            "round":   retries_now,
            "files":   changed,
            "it_pass": it_passed,
        }
        print(f"  [{name}/reviewer] 语义审查…（改动 {len(changed)} 文件，内测{'✅' if it_passed else '❌'}，第 {retries_now} 轮）")
        res = await run_agent(prompt=taskmsg, smart_level=cfg.reviewer_level, cwd=cfg.workspace,
                              readonly_dirs=cfg.readonly_dirs,
                              allowed_tools=["Read", "Bash", "Write"], system_prompt=longterm,
                              trace_name=f"{name}/reviewer")

        # reviewer 调用本身失败 → 系统故障，直接 escalate，不重试
        if not res.ok or is_usage_limit_result(res):
            reason = f"reviewer 调用失败: {res.error}"
            print(f"  [{name}/reviewer] ❌ {reason}")
            log_entry.update({"verdict": "escalate", "reason": reason, "raw": ""})
            _append_reviewer_log(cfg.workspace, log_entry)
            return {"review": ReviewResult(approved=False, action="escalate", reason=reason)}

        log_entry["raw"] = (res.text or "")[:2000]

        limit_markers = (
            "session limit", "hit your session limit", "rate limit",
            "quota", "额度", "limit reached",
        )
        review_json_path = Path(cfg.workspace) / ".review_result.json"
        if not review_json_path.exists() and any(
            marker in (res.text or "").lower() for marker in limit_markers
        ):
            reason = "reviewer 额度/会话限制，未产出 .review_result.json，必须人工处理"
            print(f"  [{name}/reviewer] ❌ {reason}")
            log_entry.update({"verdict": "escalate", "reason": reason})
            _append_reviewer_log(cfg.workspace, log_entry)
            return {"review": ReviewResult(approved=False, action="escalate", reason=reason)}

        # JSON 解析；失败 → escalate（代码处理代码，不猜自然语言）
        action, reason, viols, suggs = _read_review_json(cfg.workspace)
        if _review_retry_is_only_ignored_noise(action, reason, viols):
            ignored_reason = reason
            action = "approve"
            reason = (
                "语义审查原始打回仅包含系统元数据文件或未声明行数限制；"
                "机械闸和 run_contract 已通过，自动放行"
            )
            viols = []
            suggs = [
                *suggs,
                f"已忽略 reviewer 非契约 retry 理由：{ignored_reason}",
            ]
        log_entry.update({"verdict": action, "reason": reason,
                          "violations": viols, "suggestions": suggs})
        _append_reviewer_log(cfg.workspace, log_entry)   # ← 实时落盘

        print(f"  [{name}/reviewer] → verdict: {action} · {reason[:100]}")
        if viols:
            for v in viols:
                print(f"    ⚠ 违规: {v}")
        if suggs:
            for s in suggs:
                print(f"    💡 建议: {s}")
        return {"review": ReviewResult(approved=(action == "approve"), action=action,
                                       reason=reason, violations=viols, suggestions=suggs)}

    return planner, workers, internal_tester, reviewer


# ─────────────────── 工厂：cfg → 子图 ────────────────────────────────────────
def build_code_module(cfg: ModuleConfig, checkpointer=None):
    """给一个 ModuleConfig，返回编译好的子图。dev_mode=True 时传入 MemorySaver。"""
    async def _stub_internal_tester(state: ModuleState) -> ModuleState:
        return {"internal_test_passed": True, "internal_test_logs": "(stub)"}

    if cfg.real:
        planner, workers, internal_tester, reviewer = _build_real_nodes(cfg)
    else:
        planner, workers, internal_tester, reviewer = (
            _stub_planner, _stub_workers, _stub_internal_tester, _stub_reviewer)

    def route_after_review(state: ModuleState) -> str:
        r = state.get("review", ReviewResult())
        if r.approved or r.action in ("escalate", "reject"):
            return "report"
        if state.get("retries", 0) >= cfg.max_retries:
            return "report"
        if r.action == "retry_planner":
            return "planner"
        return "workers"

    def route_after_planner(state: ModuleState) -> str:
        return "report" if state.get("terminal_error") else "workers"

    def route_after_workers(state: ModuleState) -> str:
        return "report" if state.get("terminal_error") else "internal_tester"

    def _ws_report(state: ModuleState) -> ModuleState:
        """workspace-aware report：从 git diff 取实际落盘文件，不靠 coding_results。"""
        r = state.get("review", ReviewResult())
        terminal_error = state.get("terminal_error", "")
        # 以 workspace git status 为准（真实落盘文件），不信 coding_results 里的文件名
        changed = [] if terminal_error else _changed_files(
            cfg.workspace, cfg.name, state.get("workspace_baseline"))
        if not changed:
            # workspace 无改动（worker 失败/stub 模式）→ 退回 coding_results
            changed = list(dict.fromkeys(
                f for cr in state.get("coding_results", []) for f in cr.changed_files
            ))
        retry_exhausted = (
            not r.approved
            and not terminal_error
            and state.get("retries", 0) >= cfg.max_retries
        )
        failure_note = r.reason
        if retry_exhausted:
            failure_note = f"retry exhausted after {state.get('retries', 0)} attempts: {failure_note}"
        report = ModuleReport(
            module=state["module"], ok=r.approved,
            summary=(terminal_error or failure_note if retry_exhausted
                     else f"{state['module']} 完成 {len(state.get('subtasks', []))} 个子任务"),
            changed_files=changed,
            acceptance_for_test=[ac for st in state.get("subtasks", [])
                                  for ac in st.acceptance_criteria],
            note="" if r.approved else failure_note,
            violations=r.violations,
            suggestions=r.suggestions,
        )
        mod = state["module"]
        ok_tag = "✅" if report.ok else "❌"
        print(f"  [{mod}/report] {ok_tag} ok={report.ok} | 改动 {len(changed)} 文件: {changed}")
        if report.violations:
            print(f"  [{mod}/report] ⚠ 违规记录:")
            for v in report.violations:
                print(f"    - {v}")
        if report.suggestions:
            print(f"  [{mod}/report] 💡 建议:")
            for s in report.suggestions:
                print(f"    - {s}")
        devlog.log_event(f"{mod}/report ok={report.ok} changed={changed}")
        if not report.ok:
            devlog.log_output(f"{mod}/report failure", report.note or r.reason,
                              detailed=False)
        return {"report": report}

    g = StateGraph(ModuleState)
    g.add_node("planner",         planner)
    g.add_node("workers",         workers)
    g.add_node("internal_tester", internal_tester)
    g.add_node("reviewer",        reviewer)
    g.add_node("report",          _ws_report if cfg.real else _make_report)
    g.add_edge(START, "planner")
    g.add_conditional_edges("planner", route_after_planner,
                            {"workers": "workers", "report": "report"})
    g.add_conditional_edges("workers", route_after_workers,
                            {"internal_tester": "internal_tester", "report": "report"})
    g.add_edge("internal_tester", "reviewer")
    g.add_conditional_edges("reviewer", route_after_review,
                            {"report": "report", "workers": "workers", "planner": "planner"})
    g.add_edge("report", END)
    return g.compile(checkpointer=checkpointer)


# ─────────────────── 工作区 & 单跑入口 ────────────────────────────────────────
def ensure_workspace(path: str) -> str:
    ws = Path(path).expanduser()
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / ".git").exists():
        subprocess.run(["git", "-C", str(ws), "init", "-q"], check=False)
        (ws / ".gitkeep").touch()
        subprocess.run(["git", "-C", str(ws), "add", "-A"], check=False)
        subprocess.run(["git", "-C", str(ws), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "baseline"], check=False)
    return str(ws)


async def run_module(cfg: ModuleConfig, task: Task):
    """单独跑一个模块（不接顶层图）。dev_mode=True 时在 planner 后暂停等人审批。"""
    from ..engine.agent import shutdown
    ws = ensure_workspace(cfg.workspace)
    cfg.workspace = ws
    mode_tag = "dev_mode⏸" if cfg.dev_mode else f"真LLM={cfg.real}"
    print(f"模块：{cfg.name} | 工作区：{ws} | {mode_tag}\n任务：{task.title}\n" + "─" * 60)
    init_state = {"module": cfg.name, "task": task,
                  "workspace": ws, "retries": 0, "assignments": {}}

    if cfg.dev_mode:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        from langgraph.types import Command
        from ..contracts import ContractCheck, ContractFailure, ContractReport, ExecutionContract
        checkpointer = MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=[
            Task, CodingResult, ReviewResult, ModuleReport,
            ExecutionContract, ContractCheck, ContractFailure, ContractReport,
        ]))
        app = build_code_module(cfg, checkpointer=checkpointer)
        thread = {"configurable": {"thread_id": f"{cfg.name}-{task.id}"}}
        async def _stream(command) -> None:
            async for _ in app.astream(command, thread, stream_mode="updates"):
                pass

        await _stream(init_state)
        while True:
            snap = await app.aget_state(thread)
            if not snap.next:
                break
            interrupt_val = {}
            for t in (snap.tasks or []):
                if hasattr(t, "interrupts") and t.interrupts:
                    interrupt_val = t.interrupts[0].value; break
            if "plan" not in interrupt_val:
                print(f"[{cfg.name}/planner] ⚠ 未知 interrupt，停止单模块运行：{interrupt_val}")
                break
            plan = interrupt_val.get("plan", [])
            log_path = interrupt_val.get("log_path", "")
            print("\n" + "═" * 60)
            print(f"⏸  [{cfg.name}/planner] 方案审批（完整日志：{log_path}）\n")
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
            answer = input("\n批准此方案并继续？[y=批准 / n=拒绝并重新拆]: ").strip().lower()
            if answer == "y":
                decision = {"decision": "approve", "reason": ""}
            else:
                decision = {
                    "decision": "reject",
                    "reason": input("拒绝原因（将提供给 planner，可留空）: ").strip(),
                }
            print(f"  → {decision}\n" + "─" * 60)
            await _stream(Command(resume=decision))
        final = dict((await app.aget_state(thread)).values)
    else:
        app = build_code_module(cfg)
        final = await app.ainvoke(init_state)

    await shutdown()
    rep = final.get("report")
    print("\n" + "═" * 60)
    if rep:
        print(f"报告：ok={rep.ok}\n  摘要：{rep.summary}\n  改动：{rep.changed_files}")
        if rep.note:
            print(f"  ⚠ {rep.note}")
    print(f"代码在：{ws}")
    return final
