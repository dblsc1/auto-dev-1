"""runner.py — mechanical checks for ExecutionContract."""

from __future__ import annotations

import re
import subprocess
import hashlib
from pathlib import Path
from typing import Any

from .schemas import ContractCheck, ContractFailure, ContractReport, ExecutionContract


_TARGET_KEYS: dict[str, set[str]] = {
    "file": {"allowed", "forbidden"},
    "function": {"signature", "name"},
    "route": {"method", "path", "handler"},
    "schema": {"fields"},
    "dom": {"ids", "classes", "selectors"},
    "fetch": {"path", "fields", "forbidden_paths"},
    "consumes": {
        "provider", "path", "method", "methods", "require_any", "allow_only",
    },
}


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
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        snapshot[rel] = digest
    return snapshot


def _git_changed_files(workspace: str, baseline: dict[str, str] | None = None) -> list[str]:
    if baseline is not None:
        current = _workspace_snapshot(workspace)
        return sorted(
            path for path in set(current) | set(baseline)
            if current.get(path) != baseline.get(path)
        )
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []

    files: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) <= 3:
            continue
        path = line[3:].strip().strip('"')
        if "__pycache__" in path or path.endswith(".pyc"):
            continue
        if path.startswith("contracts/"):
            continue
        name = path.split("/")[-1]
        if name.startswith("."):
            continue
        files.append(path)
    return files


def _read_sources(workspace: str) -> tuple[dict[str, str], dict[str, str], str]:
    root = Path(workspace)
    py_sources = {
        str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="replace")
        for p in root.glob("**/*.py")
        if "__pycache__" not in str(p)
    }
    web_sources = {
        str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="replace")
        for suffix in ("*.html", "*.js", "*.ts")
        for p in root.glob(f"**/{suffix}")
    }
    all_text = "\n".join([*py_sources.values(), *web_sources.values()])
    return py_sources, web_sources, all_text


def _is_required(check: ContractCheck) -> bool:
    return check.severity == "required"


def _failure(check: ContractCheck, reason: str, *, repair_target: str = "worker") -> ContractFailure:
    return ContractFailure(
        check_id=check.id,
        kind=check.kind,
        reason=reason,
        repair_target=repair_target,  # type: ignore[arg-type]
    )


def _api_items(payload: dict[str, Any], *, view: str) -> list[dict[str, Any]]:
    """Read the new shared-contract shape while accepting pre-migration files."""
    if view == "published":
        return list(
            payload.get("published_api")
            or payload.get("public_api")
            or payload.get("contracts")
            or []
        )
    return list(
        payload.get("available_api")
        or payload.get("published_api")
        or payload.get("public_api")
        or payload.get("contracts")
        or []
    )


def _consume_target(item: dict[str, Any]) -> dict[str, Any]:
    if isinstance(item.get("target"), dict):
        return dict(item["target"])
    return {
        key: value
        for key, value in item.items()
        if key not in {"id", "severity", "rationale", "target"}
    }


def _target_definition_failures(check: ContractCheck) -> list[ContractFailure]:
    allowed = _TARGET_KEYS.get(check.kind)
    if allowed is None:
        return [
            _failure(
                check,
                f"unsupported contract check kind {check.kind!r}",
                repair_target="planner",
            )
        ]

    target = check.target or {}
    unknown = sorted(set(target) - allowed)
    if unknown:
        return [
            _failure(
                check,
                f"{check.kind} check has unsupported target fields {unknown}; "
                f"allowed={sorted(allowed)}",
                repair_target="planner",
            )
        ]

    if check.kind == "function":
        if not (target.get("signature") or target.get("name")):
            return [_failure(check, "function check needs signature or name", repair_target="planner")]
    elif check.kind == "route":
        if not target.get("path") or not target.get("method"):
            return [_failure(check, "route check needs method and path", repair_target="planner")]
    elif check.kind == "schema":
        if not target.get("fields"):
            return [_failure(check, "schema check needs non-empty fields", repair_target="planner")]
    elif check.kind == "dom":
        if not any(target.get(key) for key in ("ids", "classes", "selectors")):
            return [_failure(
                check,
                "dom check needs ids, classes, or selectors; "
                "do not use elements/required_elements",
                repair_target="planner",
            )]
    elif check.kind == "fetch":
        paths = target.get("forbidden_paths") or []
        if not target.get("path") and not paths:
            return [_failure(
                check,
                "fetch check needs path or forbidden_paths",
                repair_target="planner",
            )]
        if paths and not isinstance(paths, list):
            return [_failure(check, "fetch.forbidden_paths must be a list", repair_target="planner")]
    elif check.kind == "consumes":
        if not target.get("provider"):
            return [_failure(check, "consumes check needs provider", repair_target="planner")]
    return []


def _signature_language_failure(
    check: ContractCheck, implementation_language: str,
) -> ContractFailure | None:
    signature = str(check.target.get("signature") or "")
    if not signature or implementation_language == "generic":
        return None
    if implementation_language == "javascript":
        if re.search(r"\b(?:async\s+)?def\s+", signature):
            return _failure(
                check,
                f"frontend JavaScript contract cannot use Python signature {signature!r}",
                repair_target="planner",
            )
        if not re.search(r"\b(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(", signature):
            return _failure(
                check,
                f"JavaScript function signature is invalid: {signature!r}",
                repair_target="planner",
            )
    elif implementation_language == "python":
        if not re.search(r"\b(?:async\s+)?def\s+[A-Za-z_]\w*\s*\(", signature):
            return _failure(
                check,
                f"Python function signature is invalid: {signature!r}",
                repair_target="planner",
            )
    return None


def validate_execution_contract(
    contract: ExecutionContract | None,
    *,
    shared_contracts: dict[str, Any] | None = None,
    implementation_language: str = "generic",
) -> ContractReport:
    """Validate the planner-owned shape before checking implementation evidence.

    A contract containing only ``files_allowed`` is not an implementation
    contract: it cannot prove any requested behavior.  Mark those defects as
    planner repairs so workers are never asked to guess missing requirements.
    """
    if contract is None:
        return ContractReport(
            passed=False,
            failures=[ContractFailure(
                check_id="execution-contract",
                kind="contract",
                reason="missing execution_contract",
                repair_target="planner",
            )],
            logs="❌ execution-contract [contract] repair_target=planner missing execution_contract",
        )

    failures: list[ContractFailure] = []
    if not contract.module:
        failures.append(ContractFailure(
            check_id="contract-module", kind="contract",
            reason="execution_contract.module is empty", repair_target="planner",
        ))

    file_checks = [c for c in contract.constraints if c.kind == "file"]
    if not any(c.target.get("allowed") for c in file_checks):
        failures.append(ContractFailure(
            check_id="contract-files", kind="contract",
            reason="execution_contract needs a required file constraint with allowed paths",
            repair_target="planner",
        ))

    if not (contract.public_api or contract.internal or contract.consumes):
        failures.append(ContractFailure(
            check_id="contract-behavior", kind="contract",
            reason="execution_contract has no public_api, internal, or consumes checks",
            repair_target="planner",
        ))

    for check in [*contract.constraints, *contract.internal]:
        failures.extend(_target_definition_failures(check))
        if check.kind == "function":
            language_failure = _signature_language_failure(check, implementation_language)
            if language_failure:
                failures.append(language_failure)

    for index, item in enumerate(contract.public_api, 1):
        method = str(item.get("method") or "").upper()
        path = str(item.get("path") or "")
        if not method or not path.startswith("/"):
            failures.append(ContractFailure(
                check_id=f"public-api-{index}",
                kind="public_api",
                reason="public_api item needs an HTTP method and a path starting with '/'",
                repair_target="planner",
            ))

    shared_contracts = shared_contracts or {}
    backend = shared_contracts.get("backend") or {}
    published_backend_api = _api_items(backend, view="published")
    if contract.module == "frontend" and published_backend_api:
        backend_consumes = [
            item for item in contract.consumes
            if str(item.get("provider") or (item.get("target") or {}).get("provider") or "") == "backend"
        ]
        if not backend_consumes:
            failures.append(ContractFailure(
                check_id="frontend-backend-consumes", kind="contract",
                reason="frontend must declare the backend API it consumes",
                repair_target="planner",
            ))
        for index, item in enumerate(backend_consumes, 1):
            target = _consume_target(item)
            consume_check = ContractCheck(
                id=str(item.get("id") or f"backend-consumes-{index}"),
                kind="consumes",
                target=dict(target),
            )
            failures.extend(_target_definition_failures(consume_check))
            path = str(target.get("path") or "")
            method = str(target.get("method") or "").upper()
            if not path or not method:
                failures.append(_failure(
                    consume_check,
                    "frontend backend consumes must declare exact method and path",
                    repair_target="planner",
                ))
                continue
            if not any(
                str(api.get("method") or "").upper() == method
                and str(api.get("path") or "") == path
                for api in published_backend_api
            ):
                failures.append(_failure(
                    consume_check,
                    f"backend published_api does not expose {method} {path}",
                    repair_target="planner",
                ))

    logs = [f"validate_contract module={contract.module or '(empty)'}"]
    if failures:
        logs.extend(
            f"❌ {failure.check_id} [{failure.kind}] repair_target={failure.repair_target} {failure.reason}"
            for failure in failures
        )
    else:
        logs.append("✅ execution_contract definition")
    return ContractReport(passed=not failures, failures=failures, logs="\n".join(logs))


def _consume_check_from_item(item: dict[str, Any], index: int) -> ContractCheck:
    """Turn the serializable ``consumes`` section into a mechanical check."""
    target = _consume_target(item)
    return ContractCheck(
        id=str(item.get("id") or f"consumes-{index}"),
        kind="consumes",
        severity=str(item.get("severity") or "required"),  # type: ignore[arg-type]
        target=dict(target),
        rationale=str(item.get("rationale") or "frontend dependency on shared API"),
    )


def _check_file(
    check: ContractCheck,
    workspace: str,
    baseline: dict[str, str] | None = None,
) -> list[ContractFailure]:
    allowed = set(str(x).lstrip("./") for x in check.target.get("allowed", []) or [])
    forbidden = set(str(x).lstrip("./") for x in check.target.get("forbidden", []) or [])
    changed = set(_git_changed_files(workspace, baseline))
    failures: list[ContractFailure] = []

    if allowed:
        out_of_scope = sorted(f for f in changed if f not in allowed)
        if out_of_scope:
            failures.append(_failure(
                check,
                f"changed files outside allowed list: {out_of_scope}; allowed={sorted(allowed)}",
            ))
    touched_forbidden = sorted(changed & forbidden)
    if touched_forbidden:
        failures.append(_failure(check, f"changed forbidden files: {touched_forbidden}"))
    return failures


def _signature_name(signature: str) -> str:
    patterns = [
        r"\basync\s+def\s+([A-Za-z_]\w*)\s*\(",
        r"\bdef\s+([A-Za-z_]\w*)\s*\(",
        r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(",
        r"\bconst\s+([A-Za-z_$][\w$]*)\s*=",
    ]
    for pat in patterns:
        m = re.search(pat, signature)
        if m:
            return m.group(1)
    return signature.strip()


def _check_function(check: ContractCheck, all_text: str) -> list[ContractFailure]:
    signature = str(check.target.get("signature") or "")
    name = str(check.target.get("name") or _signature_name(signature))
    if not name:
        return [_failure(check, "function check has no name/signature", repair_target="planner")]

    patterns = [
        r"\basync\s+def\s+" + re.escape(name) + r"\s*\(",
        r"\bdef\s+" + re.escape(name) + r"\s*\(",
        r"\bfunction\s+" + re.escape(name) + r"\s*\(",
        r"\bconst\s+" + re.escape(name) + r"\s*=",
        r"\b" + re.escape(name) + r"\s*:\s*function\b",
    ]
    if any(re.search(p, all_text) for p in patterns):
        return []
    return [_failure(check, f"function {name!r} not found")]


def _check_route(check: ContractCheck, py_sources: dict[str, str], web_sources: dict[str, str]) -> list[ContractFailure]:
    path = str(check.target.get("path") or "")
    method = str(check.target.get("method") or "").lower()
    if not path:
        return [_failure(check, "route check has no path", repair_target="planner")]

    py_decorator_route_pat = re.compile(
        r'@\w+\.' + (re.escape(method) if method else r'\w+') +
        r'\s*\(\s*["\']' + re.escape(path) + r'["\']',
        re.IGNORECASE,
    )
    py_callable_route_pat = re.compile(
        r'\b\w+\.' + (re.escape(method) if method else r'\w+') +
        r'\s*\(\s*["\']' + re.escape(path) + r'["\']\s*\)\s*\(',
        re.IGNORECASE,
    )
    js_fetch_pat = re.compile(
        r'''fetch\s*\(\s*[`"']([^`"']*''' + re.escape(path) + r'''[^`"']*)[`"']''',
        re.IGNORECASE,
    )

    if any(
        py_decorator_route_pat.search(src) or py_callable_route_pat.search(src)
        for src in py_sources.values()
    ):
        return []
    if any(js_fetch_pat.search(src) for src in web_sources.values()):
        return []
    return [_failure(check, f"route/fetch path {path!r} with method {method or '*'} not found")]


def _check_schema(check: ContractCheck, all_text: str) -> list[ContractFailure]:
    fields = check.target.get("fields") or {}
    if isinstance(fields, list):
        field_names = [str(x) for x in fields]
    else:
        field_names = [str(x) for x in dict(fields).keys()]
    missing = []
    for field in field_names:
        pat = re.compile(r"\b" + re.escape(field) + r"\s*[=:]", re.IGNORECASE)
        if not pat.search(all_text):
            missing.append(field)
    if missing:
        return [_failure(check, f"schema fields not found: {missing}")]
    return []


def _check_fetch(check: ContractCheck, web_sources: dict[str, str]) -> list[ContractFailure]:
    path = str(check.target.get("path") or "")
    fields = [str(x) for x in check.target.get("fields", []) or []]
    forbidden_paths = [str(x) for x in check.target.get("forbidden_paths", []) or []]
    all_web = "\n".join(web_sources.values())
    failures: list[ContractFailure] = []
    if path and ("fetch(" not in all_web or path not in all_web):
        failures.append(_failure(check, f"frontend fetch for path {path!r} not found"))
    touched_forbidden = [item for item in forbidden_paths if item and item in all_web]
    if touched_forbidden:
        failures.append(_failure(
            check,
            f"frontend runtime code references forbidden fetch paths {touched_forbidden}",
        ))
    if failures:
        return failures
    if not path:
        return []
    if "fetch(" not in all_web or path not in all_web:
        return [_failure(check, f"frontend fetch for path {path!r} not found")]
    missing_fields = [
        field for field in fields
        if not re.search(r"\b" + re.escape(field) + r"\b", all_web)
    ]
    if missing_fields:
        return [_failure(check, f"fetch payload fields not found: {missing_fields}")]
    return []


def _check_dom(check: ContractCheck, web_sources: dict[str, str]) -> list[ContractFailure]:
    selectors = [str(x) for x in check.target.get("selectors", []) or []]
    ids = [str(x) for x in check.target.get("ids", []) or []]
    classes = [str(x) for x in check.target.get("classes", []) or []]
    all_web = "\n".join(web_sources.values())
    missing: list[str] = []

    for selector in selectors:
        if selector.startswith("#"):
            ids.append(selector[1:])
        elif selector.startswith("."):
            classes.append(selector[1:])
        elif selector and selector not in all_web:
            missing.append(selector)
    for dom_id in ids:
        if not re.search(r'\bid\s*=\s*["\']' + re.escape(dom_id) + r'["\']', all_web):
            missing.append(f"#{dom_id}")
    for cls in classes:
        if not re.search(r'\bclass\s*=\s*["\'][^"\']*\b' + re.escape(cls) + r'\b', all_web):
            missing.append(f".{cls}")
    if missing:
        return [_failure(check, f"DOM targets not found: {missing}")]
    return []


def _check_consumes(
    check: ContractCheck,
    contract: ExecutionContract,
    shared_contracts: dict[str, Any],
    web_sources: dict[str, str],
) -> list[ContractFailure]:
    provider = str(check.target.get("provider") or "")
    path = str(check.target.get("path") or "")
    if not provider:
        return [_failure(check, "consumes check has no provider", repair_target="planner")]
    provider_contract = shared_contracts.get(provider) or {}
    published_api_items = _api_items(provider_contract, view="published")
    available_api_items = _api_items(provider_contract, view="available")
    methods = {str(m).upper() for m in check.target.get("methods", []) or []}
    explicit_method = str(check.target.get("method") or "").upper()
    if explicit_method:
        methods.add(explicit_method)
    if methods:
        published_api_items = [
            item for item in published_api_items
            if str(item.get("method") or "").upper() in methods
        ]
    allowed_paths = {str(item.get("path") or "") for item in available_api_items if item.get("path")}
    if path and not any(str(item.get("path") or "") == path for item in published_api_items):
        return [_failure(check, f"shared contract {provider!r} does not expose path {path!r}",
                         repair_target="planner")]

    all_web = "\n".join(web_sources.values())
    fetch_args = re.findall(r'''fetch\s*\(\s*[`"']([^`"']+)[`"']''', all_web, re.IGNORECASE)
    failures: list[ContractFailure] = []

    if path and all_web and path not in all_web:
        failures.append(_failure(check, f"consumer does not fetch required path {path!r}"))

    if check.target.get("require_any") and allowed_paths:
        if not any(path_item in all_web for path_item in allowed_paths):
            failures.append(_failure(
                check,
                f"consumer does not fetch any {provider!r} public API path {sorted(allowed_paths)}",
            ))

    if check.target.get("allow_only", True) and fetch_args and allowed_paths:
        unknown = [
            arg for arg in fetch_args
            if not any(path_item and path_item in arg for path_item in allowed_paths)
        ]
        if unknown:
            failures.append(_failure(
                check,
                f"consumer fetches paths not in {provider!r} shared contract: {unknown}",
                repair_target="worker",
            ))
    if failures:
        return failures
    return []


def run_contract(
    workspace: str,
    contract: ExecutionContract | None,
    shared_contracts: dict[str, Any] | None = None,
    implementation_language: str = "generic",
    workspace_baseline: dict[str, str] | None = None,
) -> ContractReport:
    shared_contracts = shared_contracts or {}
    definition_report = validate_execution_contract(
        contract,
        shared_contracts=shared_contracts,
        implementation_language=implementation_language,
    )
    if not definition_report.passed or contract is None:
        return definition_report

    py_sources, web_sources, all_text = _read_sources(workspace)
    failures: list[ContractFailure] = []
    warnings: list[str] = []
    log_lines = [*definition_report.logs.splitlines(),
                 f"run_contract module={contract.module} version={contract.version}"]

    checks = [
        *contract.constraints,
        *contract.internal,
        *[_consume_check_from_item(item, index)
          for index, item in enumerate(contract.consumes, 1)
          if isinstance(item, dict)],
    ]
    for item in contract.public_api:
        if item.get("path"):
            checks.append(ContractCheck(
                id=f"public-api:{item.get('id') or item.get('path')}",
                kind="route",
                target={"method": item.get("method"), "path": item.get("path")},
                rationale="public_api must be implemented by module code",
            ))
            request = item.get("request") or {}
            if request:
                checks.append(ContractCheck(
                    id=f"public-api-schema:{item.get('id') or item.get('path')}",
                    kind="schema",
                    target={"fields": request},
                    rationale="public_api request fields must be present in code",
                ))

    for check in checks:
        check_failures: list[ContractFailure]
        if check.kind == "file":
            check_failures = _check_file(check, workspace, workspace_baseline)
        elif check.kind == "function":
            check_failures = _check_function(check, all_text)
        elif check.kind == "route":
            check_failures = _check_route(check, py_sources, web_sources)
        elif check.kind == "schema":
            check_failures = _check_schema(check, all_text)
        elif check.kind == "fetch":
            check_failures = _check_fetch(check, web_sources)
        elif check.kind == "dom":
            check_failures = _check_dom(check, web_sources)
        elif check.kind == "consumes":
            check_failures = _check_consumes(check, contract, shared_contracts, web_sources)
        else:
            check_failures = []
            warnings.append(f"{check.id}: unsupported contract check kind {check.kind!r}")

        if check_failures and _is_required(check):
            failures.extend(check_failures)
            for f in check_failures:
                log_lines.append(
                    f"❌ {check.id} [{check.kind}] repair_target={f.repair_target} {f.reason}"
                )
        elif check_failures:
            for f in check_failures:
                warnings.append(f"{check.id}: {f.reason}")
                log_lines.append(
                    f"⚠ {check.id} [{check.kind}] repair_target={f.repair_target} {f.reason}"
                )
        else:
            log_lines.append(f"✅ {check.id} [{check.kind}]")

    if warnings:
        log_lines.append("Warnings:")
        log_lines.extend(f"  - {w}" for w in warnings)

    return ContractReport(
        passed=not failures,
        failures=failures,
        warnings=warnings,
        logs="\n".join(log_lines),
    )
