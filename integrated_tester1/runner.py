"""integrated_tester1/runner.py — 程序化内部测试执行器。

根据 InternalTestStandard.all_checks 决定跑哪些检查。
由 code_module 的 internal_tester 节点调用（workers 完成后、reviewer 之前）。

返回 (passed: bool, report: str)，report 会注入 reviewer 的 notes 上下文。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from ..contracts import ContractReport, ExecutionContract, run_contract
from .standard import InternalTestStandard


async def run_checks(
    workspace: str,
    standard: InternalTestStandard,
    timeout: int = 60,
    subtasks=None,          # list[Task] — 用于 api_contract 检查
    execution_contract: ExecutionContract | None = None,
    shared_contracts: dict | None = None,
    implementation_language: str = "generic",
    workspace_baseline: dict[str, str] | None = None,
) -> tuple[bool, str, ContractReport | None]:
    """返回 (all_passed, combined_report, contract_report)。"""
    checks = standard.all_checks
    if not checks and execution_contract is None:
        return True, "（无内部测试项，跳过）", None

    sections: list[str] = []
    all_passed = True
    contract_report: ContractReport | None = None

    if "compile" in checks or "pytest" in checks:
        passed, log = await _run_pytest(workspace, timeout)
        tag = "✅" if passed else "❌"
        sections.append(f"[pytest] {tag}\n{log.strip()[-1500:]}")
        if not passed:
            all_passed = False

    if "dom_check" in checks:
        passed, log = _run_dom_check(workspace)
        tag = "✅" if passed else "❌"
        sections.append(f"[dom_check] {tag}\n{log}")
        if not passed:
            all_passed = False

    if "api_contract" in checks and execution_contract is None:
        passed, log = _run_api_contract_check(workspace, subtasks or [])
        tag = "✅" if passed else "❌"
        sections.append(f"[api_contract] {tag}\n{log}")
        if not passed:
            all_passed = False

    if execution_contract is not None:
        contract_report = run_contract(
            workspace,
            execution_contract,
            shared_contracts=shared_contracts or {},
            implementation_language=implementation_language,
            workspace_baseline=workspace_baseline,
        )
        tag = "✅" if contract_report.passed else "❌"
        sections.append(f"[run_contract] {tag}\n{contract_report.logs}")
        if not contract_report.passed:
            all_passed = False

    # 未来扩展：typecheck / lint
    for check in sorted(checks - {"compile", "pytest", "dom_check", "api_contract"}):
        sections.append(f"[{check}] ⏭ 未实现，跳过")

    report = "\n\n".join(sections)
    return all_passed, report, contract_report


# ── pytest ───────────────────────────────────────────────────────────────────

async def _run_pytest(workspace: str, timeout: int) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        log = stdout.decode(errors="replace")
        # exit 5 = no tests collected → 视为通过（没有测试≠失败）
        passed = proc.returncode in (0, 5)
        return passed, log
    except asyncio.TimeoutError:
        return False, f"pytest 超时（>{timeout}s）"
    except FileNotFoundError:
        return False, f"workspace 不存在或 pytest 未安装：{workspace}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── dom_check ─────────────────────────────────────────────────────────────────

def _run_dom_check(workspace: str) -> tuple[bool, str]:
    html_files = list(Path(workspace).glob("**/*.html"))
    if not html_files:
        return True, "无 HTML 文件，跳过 dom_check"

    issues: list[str] = []
    for html_file in html_files:
        try:
            content = html_file.read_text(encoding="utf-8", errors="replace")
            file_issues = _check_html(content, html_file.name)
            issues.extend(file_issues)
        except Exception as e:
            issues.append(f"{html_file.name}: 读取失败 → {e}")

    if issues:
        return False, "DOM 检查发现以下问题：\n" + "\n".join(f"  · {i}" for i in issues)
    return True, f"DOM 检查通过（共 {len(html_files)} 个 HTML 文件）"


def _check_html(content: str, filename: str) -> list[str]:
    issues: list[str] = []

    # ① inline style.display 和 className 冲突：inline 优先级高于 class
    if re.search(r"style\.display\s*=\s*['\"]none['\"]", content):
        if re.search(r"\.className\s*=", content):
            issues.append(
                f"{filename}: inline style.display='none' 与 className 切换共存 — "
                "inline style 优先级高于 CSS class，className 里的 display:block 会被覆盖。"
                "建议：删除 style.display 赋值，统一用 className 控制显隐。"
            )

    # ② fetch 存在但没有错误处理
    if "fetch(" in content:
        has_catch = bool(re.search(r"\.catch\s*\(|catch\s*\(err|catch\s*\(e\b", content))
        has_try   = "try {" in content or "try{" in content
        if not has_catch and not has_try:
            issues.append(
                f"{filename}: fetch 调用没有错误处理（无 .catch / try-catch），"
                "网络异常时页面无任何提示。"
            )

    # ③ input 没有前端校验
    if re.search(r'<input[^>]+type=["\']number["\']', content):
        if "fetch(" in content and not re.search(
            r"(isNaN|parseInt|parseFloat|\.value\s*===\s*['\"]|\.trim\(\))", content
        ):
            issues.append(
                f"{filename}: 有 number 类型 input，但 fetch 前未见输入校验，"
                "空值或非数字可能直接发请求。"
            )

    return issues


# ── api_contract ──────────────────────────────────────────────────────────────

def _run_api_contract_check(workspace: str, subtasks: list) -> tuple[bool, str]:
    """验证 planner interface_contract 声明的路径和请求字段是否真正在代码里实现。

    后端检查：
      - path 出现在 @app.xxx("path") 或 @router.xxx("path") 装饰器里
      - request schema 字段名出现在 Pydantic 模型定义里

    前端检查：
      - interface_contract.path 出现在 fetch("/path") 调用里
    """
    ws = Path(workspace)
    issues: list[str] = []
    ok_items: list[str] = []

    # 读取 workspace 里所有源文件
    py_sources  = {f.name: f.read_text(errors="replace") for f in ws.glob("**/*.py")
                   if "__pycache__" not in str(f)}
    html_sources = {f.name: f.read_text(errors="replace") for f in ws.glob("**/*.html")}
    all_src_text = "\n".join(list(py_sources.values()) + list(html_sources.values()))

    if not all_src_text.strip():
        return True, "（workspace 无源文件，跳过 api_contract 检查）"

    contracts_checked = 0
    for st in subtasks:
        contract = getattr(st, "interface_contract", {}) or {}
        path = contract.get("path", "")
        method = (contract.get("method") or "").upper()
        request_schema = contract.get("request") or {}

        if not path:
            continue
        contracts_checked += 1

        # ── 1. 路径存在性检查 ─────────────────────────────────────────────────
        # Python：@app.post("/path") 或 @router.get("/path")
        py_route_pat = re.compile(
            r'@\w+\.' + (method.lower() if method else r'\w+') +
            r'\s*\(\s*["\']' + re.escape(path) + r'["\']',
            re.IGNORECASE
        )
        # HTML/JS：fetch("/path") 或 fetch(`${BASE}/path`)
        js_fetch_pat = re.compile(
            r'''fetch\s*\(\s*[`"']([^`"']*''' + re.escape(path) + r'''[^`"']*)[`"']''',
            re.IGNORECASE
        )

        found_in_py   = any(py_route_pat.search(src) for src in py_sources.values())
        found_in_html = any(js_fetch_pat.search(src) for src in html_sources.values())
        found_path    = found_in_py or found_in_html

        if not found_path:
            # 宽松：只要 path 字符串本身出现在任何源文件里就算找到
            if path in all_src_text:
                ok_items.append(f"  ✅ path {path!r} — 字符串存在（弱匹配）")
            else:
                issues.append(
                    f"  ❌ subtask [{st.id}] interface_contract.path={path!r} "
                    f"在 workspace 任何源文件中均未找到。"
                    f"后端应有 @app.{method.lower()}(\"{path}\") 装饰器，"
                    f"前端应有 fetch(\"{path}\")。"
                )
                continue
        else:
            ok_items.append(f"  ✅ path {path!r} — 路由/fetch 已找到")

        # ── 2. 请求字段存在性检查（仅 Python 后端）────────────────────────────
        if request_schema and py_sources:
            missing_fields = []
            for field_name in request_schema:
                # 字段名出现在 Pydantic 模型里：形如 "field_name: type" 或 "field_name ="
                field_pat = re.compile(
                    r'\b' + re.escape(field_name) + r'\s*[=:]',
                    re.IGNORECASE
                )
                if not any(field_pat.search(src) for src in py_sources.values()):
                    missing_fields.append(field_name)
            if missing_fields:
                issues.append(
                    f"  ❌ subtask [{st.id}] interface_contract.request 字段 "
                    f"{missing_fields} 在 Python 代码中找不到对应定义 "
                    f"（期望 Pydantic 模型有这些字段）。"
                )
            else:
                ok_items.append(f"  ✅ request schema 字段 {list(request_schema.keys())} — 均已定义")

    if contracts_checked == 0:
        return True, "（无 interface_contract 声明，跳过 api_contract 检查）"

    report_lines = [f"api_contract 检查（{contracts_checked} 个合约）："]
    report_lines.extend(ok_items)
    if issues:
        report_lines.append("发现以下接口不一致：")
        report_lines.extend(issues)
        return False, "\n".join(report_lines)

    return True, "\n".join(report_lines)
