"""arbiter.py — 业务层裁决：AI 拆任务 + 可向用户澄清需求 + 看结果。

arbiter_intake       : Codex（level=5）分析需求，用 Write 工具写 .arbiter_plan.json，
                       再读文件解析；彻底绕开 CC agent 输出噪声。
                       支持批次化：tasks 按依赖顺序拆成多批，逐批执行。
arbiter_batch_review : 每批任务完成后检查结果，决定继续下批还是直接跑测试。
                       记录工作留痕到 batch_logs。上限 max_batch_iterations 防死循环。
arbiter_review_test  : 通用——看测试结果拍板（复用 programmer1 里的实现）。

模型调用统一走 run_agent(smart_level=_PLANNER_LEVEL)。
文件输出：ARBITER_WORKSPACE/.arbiter_plan.json。
"""

from __future__ import annotations

import json
import datetime
from pathlib import Path

from programmer1.state   import GraphState
from programmer1.schemas import Task, ModuleReport
from programmer1.config import ARBITER_PLANNER_LEVEL
from programmer1.modules.arbiter import arbiter_review_test   # noqa: F401
from programmer1.engine.agent import is_usage_limit_result, run_agent
from programmer1.workspace_git import commit_workspace, push_workspace
from programmer1.devlog import session as devlog
from contract_cfg import ensure_contract_workspace, BACKEND_CONTRACT_FILE

# ─────────────────── 工作区 ──────────────────────────────────────────────────
ARBITER_WORKSPACE = Path.home() / "gg-workspace" / "arbiter"
_PLAN_FILE = ARBITER_WORKSPACE / ".arbiter_plan.json"

# ─────────────────── 系统提示 ─────────────────────────────────────────────────
_SYSTEM = """\
你是项目经理，负责把用户需求拆成模块级任务，按依赖顺序分批分配给编码团队。

【可用模块】
- backend : Python FastAPI 服务；files_allowed ⊆ ["main.py","requirements.txt","models.py","auth.py"]
- frontend: 纯 HTML/CSS/原生 JS，单文件优先；files_allowed ⊆ ["index.html"]

【规则】
1. 只列真正需要的模块——纯后端需求不要加 frontend，纯展示不要加 backend。
2. files_allowed 只列本次任务真正会碰的文件，不要把不相关的文件也写进去。
3. 需求描述含糊到无法确定技术方案时，设 needs_clarification=true 并给出 question。
4. 只拆产品实现任务，不要把测试、E2E、验收、Playwright、pytest 拆给 backend/frontend；
   所有编码批次完成后，系统会自动进入 test_engineer 执行测试。
5. 把任务按依赖顺序分批（batches）：同一批内的任务可以并行，下一批依赖上一批的结果。
   只要需求同时包含后端 API 和前端调用，必须 batch 0 = [backend]，batch 1 = [frontend]。
   后端是 API 契约唯一来源；后端完成后系统会把 backend planner 的 interface_contract
   固化到 contracts/backend_api.json：published_api 是本次新接口，available_api 是后端现有完整接口面。
   frontend 必须读取该契约，不得自行发明 API。
   简单需求可以只有一批。
6. 用 Write 工具把方案写入当前目录的 .arbiter_plan.json，内容是严格 UTF-8 JSON。
   【重要】所有字段值里绝对不能出现未转义的英文双引号 "，引用名词请用 「」 或 【】。
   {
     "needs_clarification": false,
     "question": "",
     "batches": [
       [
         {
           "id": "be-1",
           "owner": "backend",
           "title": "简短标题",
           "description": "给工程师的详细说明",
           "complexity": "simple|medium|complex",
           "acceptance_criteria": ["验收标准1"],
           "files_allowed": ["main.py"]
         }
       ],
       [
         {
           "id": "fe-1",
           "owner": "frontend",
           "title": "前端页面",
           "description": "调用后端 API，展示结果",
           "complexity": "simple",
           "acceptance_criteria": ["页面加载正常"],
           "files_allowed": ["index.html"]
         }
       ]
     ]
   }
7. 写完后输出一行"已写入 .arbiter_plan.json"作为确认，不要输出别的内容。
"""

_PLANNER_LEVEL = ARBITER_PLANNER_LEVEL


# ─────────────────── 辅助 ─────────────────────────────────────────────────────

def _read_plan() -> dict:
    try:
        text = _PLAN_FILE.read_text(encoding="utf-8")
    except Exception:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # CC agent 偶尔在字符串里写未转义的 "，用 json-repair 容错修复
        try:
            from json_repair import loads as repair_loads
            return repair_loads(text)
        except Exception:
            return {}


def _parse_task(t: dict) -> Task:
    owner = t.get("owner", "backend")
    default_files = (
        ["main.py", "requirements.txt"] if owner == "backend" else ["index.html"]
    )
    return Task(
        id=t.get("id", f"{owner}-1"),
        title=t.get("title", "编码任务"),
        description=t.get("description", ""),
        complexity=t.get("complexity", "medium"),
        owner=owner,
        acceptance_criteria=t.get("acceptance_criteria", []),
        files_allowed=t.get("files_allowed") or default_files,
    )


def _build_batches(data: dict) -> list[list[Task]]:
    """解析 batches 字段；兼容旧 tasks 字段（包成单批）。"""
    if "batches" in data:
        batches = []
        for batch_raw in data["batches"]:
            batch = [_parse_task(t) for t in batch_raw]
            if batch:
                batches.append(batch)
        return batches or [[]]
    # 向后兼容：旧 tasks 字段 → 单批
    flat = [_parse_task(t) for t in data.get("tasks", [])]
    return [flat] if flat else [[]]


# ─────────────────── 节点 ─────────────────────────────────────────────────────

_DEFAULT_MAX_BATCH_ITERATIONS = 10


async def arbiter_intake(state: GraphState) -> GraphState:
    """Codex 分析需求 → 写 .arbiter_plan.json → 读取解析 → 拆批次。
    需要澄清时 interrupt 问用户。"""
    from langgraph.types import interrupt

    req = state["requirement"]
    devlog.start(req, detailed=bool(state.get("dev_mode", False)))
    print(f"\n[arbiter] 收到需求 → Codex 分析中：{req}")

    ARBITER_WORKSPACE.mkdir(parents=True, exist_ok=True)
    _PLAN_FILE.unlink(missing_ok=True)

    res = await run_agent(
        prompt=f"需求：{req}",
        smart_level=_PLANNER_LEVEL,
        system_prompt=_SYSTEM,
        cwd=str(ARBITER_WORKSPACE),
        allowed_tools=["Read", "Write"],
        trace_name="arbiter/intake",
    )
    if not res.ok or is_usage_limit_result(res):
        kind = "额度/会话限制" if is_usage_limit_result(res) else "模型调用失败"
        reason = f"arbiter intake {kind}: {res.error or res.text or '无详细错误'}"
        print(f"[arbiter] ✋ {reason}")
        devlog.finish(reason)
        devlog.save()
        return {"decision": "escalate", "result": reason, "stop_reason": reason}

    data = _read_plan()
    devlog.log_output("arbiter plan", _PLAN_FILE.read_text(encoding="utf-8")
                      if _PLAN_FILE.exists() else "")

    if data.get("needs_clarification"):
        question = data.get("question") or "请描述更多需求细节："
        print(f"[arbiter] ⏸ 需要澄清：{question}")
        answer: str = interrupt({"question": question})
        print(f"[arbiter] 收到回答：{answer}")
        _PLAN_FILE.unlink(missing_ok=True)
        res2 = await run_agent(
            prompt=f"补充说明：{answer}",
            smart_level=_PLANNER_LEVEL,
            system_prompt=_SYSTEM,
            cwd=str(ARBITER_WORKSPACE),
            allowed_tools=["Read", "Write"],
            session_id=res.session_id,
            trace_name="arbiter/clarify",
        )
        if not res2.ok or is_usage_limit_result(res2):
            kind = "额度/会话限制" if is_usage_limit_result(res2) else "模型调用失败"
            reason = f"arbiter clarify {kind}: {res2.error or res2.text or '无详细错误'}"
            print(f"[arbiter] ✋ {reason}")
            devlog.finish(reason)
            devlog.save()
            return {"decision": "escalate", "result": reason, "stop_reason": reason}
        data = _read_plan()
        devlog.log_output("arbiter clarified plan", _PLAN_FILE.read_text(encoding="utf-8")
                          if _PLAN_FILE.exists() else "")

    batches = _build_batches(data)
    if not batches or not batches[0]:
        reason = "arbiter 没有产出可执行任务，停止以避免用 fallback 猜测需求"
        print(f"[arbiter] ✋ {reason}")
        devlog.finish(reason)
        devlog.save()
        return {"decision": "escalate", "result": reason, "stop_reason": reason}

    print(f"[arbiter] 共 {len(batches)} 批次：")
    for i, batch in enumerate(batches):
        for t in batch:
            print(f"    批次{i}  → {t.owner}: {t.title}（{t.complexity}）  files={t.files_allowed}")
    devlog.log_event(f"共 {len(batches)} 批次，总任务 {sum(len(b) for b in batches)} 个")
    ensure_contract_workspace()

    first_batch = batches[0]
    return {
        "task_batches":         batches,
        "batch_cursor":         0,
        "batch_logs":           [],
        "max_batch_iterations": _DEFAULT_MAX_BATCH_ITERATIONS,
        "module_tasks":         first_batch,
        "cursor":               0,
        "module_reports":       [],
        "test_retries":         0,
    }


_BATCH_REVIEW_SYSTEM = """\
你是项目经理，负责审查刚完成的一批编码任务的结果，决定下一步动作。

你会收到：
- 本批次任务清单（id、title、owner）
- 各模块的完成报告（ok=true/false，简短摘要）
- 剩余未执行的批次清单

【决策规则】
1. 若本批次所有任务 ok=true → 输出 "PROCEED"，继续下一批。
2. 若有任务 ok=false：
   a. 问题是临时性（网络、格式错误等）→ 输出 "RETRY:<id>"，重试该任务。
   b. 问题是结构性（需求不清、接口冲突）→ 输出 "ABORT: <原因>"，放弃后续批次直接测试。
3. 若无后续批次 → 直接输出 "PROCEED"。

只输出一行决策，不要输出其他内容。
"""

async def _ai_batch_decision(batch_cur: int, current_tasks, reports, remaining_batches) -> tuple[str, str]:
    """调 Codex 裁决本批结果；返回 PROCEED / RETRY:<id> / ABORT:<reason>。"""
    task_lines = "\n".join(
        f"  - {t.id} [{t.owner}] {t.title}" for t in current_tasks
    )
    report_lines = "\n".join(
        f"  - {r.module}: {'✅' if r.ok else '❌'} {getattr(r, 'summary', '')[:120]}"
        for r in reports if hasattr(r, "module")
    ) or "  （无报告）"
    remaining_lines = "\n".join(
        f"  批次{i+batch_cur+1}: " + ", ".join(t.id for t in b)
        for i, b in enumerate(remaining_batches)
    ) or "  （无后续批次）"

    prompt = f"""本批次（批次{batch_cur}）任务：
{task_lines}

执行报告：
{report_lines}

剩余批次：
{remaining_lines}

请决策。"""

    res = await run_agent(
        prompt=prompt,
        smart_level=_PLANNER_LEVEL,
        system_prompt=_BATCH_REVIEW_SYSTEM,
        cwd=str(ARBITER_WORKSPACE),
        allowed_tools=[],   # 只需输出文字，不需要工具
        trace_name="arbiter/batch_review",
    )
    if not res.ok or is_usage_limit_result(res):
        kind = "额度/会话限制" if is_usage_limit_result(res) else "模型调用失败"
        reason = f"batch review {kind}: {res.error or res.text or '无详细错误'}"
        print(f"[arbiter/batch_review] ✋ {reason}")
        devlog.log_output("arbiter/batch_review failure", reason, detailed=False)
        return "HUMAN_STOP", reason
    decision = (res.text or "").strip().split("\n")[0].strip().upper()
    if not decision:
        return "HUMAN_STOP", "batch review 未输出决策"
    print(f"[arbiter/batch_review] AI 裁决：{decision}")
    devlog.log_output("arbiter/batch_review raw decision", res.text or res.error or "")
    return decision, ""


def _requires_human_stop(report: ModuleReport) -> bool:
    text = " ".join([
        getattr(report, "note", "") or "",
        getattr(report, "summary", "") or "",
        " ".join(getattr(report, "violations", []) or []),
        " ".join(getattr(report, "suggestions", []) or []),
    ]).lower()
    markers = (
        "escalate", "reviewer 调用失败", "reviewer 输出格式故障",
        "retry exhausted",
        "session limit", "hit your session limit", "limit reached",
        "rate limit", "quota", "额度",
    )
    return any(m in text for m in markers)


async def arbiter_batch_review(state: GraphState) -> GraphState:
    """每批任务完成后：记录留痕 → AI 裁决 → 继续下批 / 重试 / 放弃进测试。"""
    batches:    list     = state.get("task_batches", [[]])
    batch_cur:  int      = state.get("batch_cursor", 0)
    batch_logs: list     = list(state.get("batch_logs", []))
    reports:    list     = state.get("module_reports", [])
    max_iter:   int      = state.get("max_batch_iterations", _DEFAULT_MAX_BATCH_ITERATIONS)

    current_tasks    = batches[batch_cur] if batch_cur < len(batches) else []
    remaining_batches = batches[batch_cur + 1:]

    # ── 留痕 ───────────────────────────────────────────────────────────────────
    devlog.log_batch(batch_cur, current_tasks, reports)
    log_entry = {
        "batch_index": batch_cur,
        "timestamp":   datetime.datetime.now().isoformat(timespec="seconds"),
        "tasks":       [{"id": t.id, "title": t.title, "owner": t.owner}
                        for t in current_tasks],
        "reports":     [{"module": r.module, "ok": r.ok,
                         "summary": getattr(r, "summary", "")[:200]}
                        for r in reports if hasattr(r, "module")],
    }
    batch_logs.append(log_entry)
    print(f"[arbiter/batch_review] 批次 {batch_cur} 完成，留痕已记录")

    next_batch_cur = batch_cur + 1

    # ── 硬门禁：任何模块 ok=false 都不能 PROCEED ─────────────────────────────
    failed_reports = [r for r in reports if hasattr(r, "ok") and not r.ok]
    if failed_reports:
        failed_text = "\n".join(
            f"- {r.module}: {getattr(r, 'note', '') or getattr(r, 'summary', '')}"
            for r in failed_reports
        )
        devlog.log_output("arbiter/batch_review failed reports", failed_text,
                          detailed=False)
        if any(_requires_human_stop(r) for r in failed_reports):
            reason = "模块失败且需要人工处理，停止流程，不测试、不提交：\n" + failed_text
            print(f"[arbiter/batch_review] ✋ {reason}")
            devlog.finish(reason)
            devlog.save()
            return {
                "decision": "escalate",
                "result": reason,
                "batch_logs": batch_logs,
                "batch_cursor": len(batches),
            }

        failed_owners = {r.module for r in failed_reports}
        retry_tasks = [t for t in current_tasks if t.owner in failed_owners]
        if retry_tasks:
            print(f"[arbiter/batch_review] ✋ ok=false → 打回重做："
                  f" {', '.join(t.id for t in retry_tasks)}")
            new_batches = list(batches)
            new_batches.insert(next_batch_cur, retry_tasks)
            return {
                "task_batches":   new_batches,
                "batch_cursor":   next_batch_cur,
                "batch_logs":     batch_logs,
                "module_tasks":   retry_tasks,
                "cursor":         0,
                "module_reports": [],
            }

        reason = "模块失败但无法定位可重试任务，停止流程：\n" + failed_text
        print(f"[arbiter/batch_review] ✋ {reason}")
        devlog.finish(reason)
        devlog.save()
        return {
            "decision": "escalate",
            "result": reason,
            "batch_logs": batch_logs,
            "batch_cursor": len(batches),
        }

    # ── 上限检查（仅在本批全部 ok 后生效）────────────────────────────────────
    over_limit = next_batch_cur >= max_iter
    if over_limit:
        print(f"[arbiter/batch_review] 已达批次上限（{max_iter}），停止流程，等待人工处理")
        reason = f"已达批次上限（{max_iter}），停止流程，避免无限重试"
        devlog.finish(reason)
        devlog.save()
        return {
            "decision": "escalate",
            "result": reason,
            "batch_logs": batch_logs,
            "batch_cursor": len(batches),
        }

    # ── AI 裁决 ────────────────────────────────────────────────────────────────
    decision, decision_error = await _ai_batch_decision(
        batch_cur, current_tasks, reports, remaining_batches)
    if decision == "HUMAN_STOP":
        reason = f"批次裁决不可用，停止流程，不测试、不提交：{decision_error}"
        devlog.finish(reason)
        devlog.save()
        return {
            "decision": "escalate", "result": reason, "stop_reason": reason,
            "batch_logs": batch_logs, "batch_cursor": len(batches),
        }

    if decision.startswith("ABORT"):
        reason = decision[5:].lstrip(":").strip() or "AI 裁决放弃后续批次"
        reason = f"{reason}；未执行批次不能直接进入测试"
        print(f"[arbiter/batch_review] ✋ ABORT → {reason}")
        devlog.finish(reason)
        devlog.save()
        return {
            "decision": "escalate", "result": reason, "stop_reason": reason,
            "batch_logs": batch_logs, "batch_cursor": len(batches),
        }

    if decision.startswith("RETRY:"):
        retry_id = decision[6:].strip()
        print(f"[arbiter/batch_review] RETRY → 重试任务 {retry_id}")
        # 找到需要重试的任务，重新作为单任务批次插入
        retry_task = next(
            (t for batch in batches for t in batch if t.id == retry_id), None
        )
        if retry_task:
            new_batches = list(batches)
            new_batches.insert(next_batch_cur, [retry_task])
            return {
                "task_batches":   new_batches,
                "batch_cursor":   next_batch_cur,
                "batch_logs":     batch_logs,
                "module_tasks":   [retry_task],
                "cursor":         0,
                "module_reports": [],
            }
        print(f"[arbiter/batch_review] 未找到任务 {retry_id}，退化为 PROCEED")

    # PROCEED（or fallback）
    if not remaining_batches:
        print("[arbiter/batch_review] 所有批次完成 → 进入测试")
        return {"batch_logs": batch_logs, "batch_cursor": next_batch_cur}

    next_tasks = batches[next_batch_cur]
    print(f"[arbiter/batch_review] PROCEED → 批次 {next_batch_cur}（{len(next_tasks)} 任务）")
    return {
        "batch_cursor":   next_batch_cur,
        "batch_logs":     batch_logs,
        "module_tasks":   next_tasks,
        "cursor":         0,
        "module_reports": [],
    }


async def arbiter_commit_push(state: GraphState) -> GraphState:
    """所有批次通过测试后：对每个 workspace 做 git commit + push（如配了 remote）。

    从 backend_cfg / frontend_cfg 读取 workspace + git_remote + git_branch。
    每个 workspace 独立操作，互不影响。
    """
    from backend_cfg  import BACKEND_CONFIG
    from frontend_cfg import FRONTEND_CONFIG

    if state.get("decision") != "merge":
        msg = "非 merge 决策，跳过 commit/push"
        print(f"[arbiter/commit_push] {msg}")
        devlog.finish(msg)
        devlog.save()
        return {"result": state.get("result", "") + f"\n\n---\n{msg}"}

    configs = [BACKEND_CONFIG, FRONTEND_CONFIG]
    push_results: list[str] = []

    for cfg in configs:
        ws     = cfg.workspace
        remote = getattr(cfg, "git_remote", "")
        branch = getattr(cfg, "git_branch", "") or cfg.name

        committed = commit_workspace(ws, f"feat({cfg.name}): AI 生成 [{state.get('requirement','')[:60]}]")
        if committed:
            push_results.append(f"✅ {cfg.name}: committed")
            if remote:
                ok = push_workspace(ws, remote, branch)
                push_results.append(f"  {'✅' if ok else '❌'} push → {remote}/{branch}")
            else:
                push_results.append(f"  ⚠ {cfg.name}: git_remote 未配置，跳过 push")
        else:
            push_results.append(f"  — {cfg.name}: 无变更，跳过")

    summary = "\n".join(push_results)
    print(f"[arbiter/commit_push]\n{summary}")

    devlog.finish("已提交" if any("✅" in r for r in push_results) else "无变更或推送失败")
    devlog.save()

    existing = state.get("result", "")
    return {"result": existing + f"\n\n---\n**Git 提交记录**\n{summary}"}
