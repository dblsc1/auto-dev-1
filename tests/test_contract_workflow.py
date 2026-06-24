"""Focused checks for contract gates and provider-failure propagation."""

from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace

from programmer1.contracts import (
    ContractCheck,
    ExecutionContract,
    execution_contract_to_dict,
    run_contract,
    validate_execution_contract,
)
from programmer1.config import configured_smart_level
from programmer1.engine.agent import (
    _normalize_result,
    fallback_level_for_provider_failure,
    is_usage_limit_result,
)
from programmer1.engine.providers.base import AgentResult
from programmer1.engine.providers.deepseek_agent import _looks_like_api_failure
from programmer1.engine.providers.glm_agent import _looks_like_auth_failure
from programmer1.engine.registry import resolve
from programmer1.modules.arbiter import arbiter_review_test
from programmer1.modules.code_module import (
    _allowed_files_for_review,
    _changed_files,
    _review_retry_is_only_ignored_noise,
    _workspace_snapshot,
)
from programmer1.schemas import Task, TestResult as WorkflowTestResult
from test_engineer1.nodes.run_mcp_tests import (
    _consumer_pacts,
    _plan_url,
    _requires_backend_service,
    _route_patterns,
    _verify_consumer_pacts,
    _url_matches,
    _run_shared_contract_check,
    run_mcp_tests,
    _start_services,
    _stop_process,
)
from test_engineer1.nodes.plan_tests import _extract_plan_json
from test_engineer1.plan_schema import validate_test_plan
from test_engineer1.proxy_server import should_proxy_path


def _backend_contract() -> ExecutionContract:
    return ExecutionContract(
        module="backend",
        public_api=[{"id": "status", "method": "GET", "path": "/status", "response_200": {"status": "ok"}}],
        constraints=[ContractCheck("files", "file", target={"allowed": ["main.py"]})],
    )


def _frontend_contract() -> ExecutionContract:
    return ExecutionContract(
        module="frontend",
        internal=[ContractCheck("status-button", "dom", target={"ids": ["status-button"]})],
        consumes=[{
            "id": "backend-status",
            "provider": "backend",
            "method": "GET",
            "path": "/status",
            "allow_only": True,
        }],
        constraints=[ContractCheck("files", "file", target={"allowed": ["index.html"]})],
    )


def test_empty_execution_contract_is_planner_failure(tmp_path):
    report = validate_execution_contract(
        ExecutionContract(
            module="frontend",
            constraints=[ContractCheck("files", "file", target={"allowed": ["index.html"]})],
        )
    )

    assert not report.passed
    assert report.failures[0].repair_target == "planner"


def test_frontend_consumes_shared_api_and_rejects_unknown_fetch(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<button id="status-button"></button><script>fetch("/status")</script>',
        encoding="utf-8",
    )
    shared = {"backend": {"published_api": _backend_contract().public_api}}

    passed = run_contract(str(frontend), _frontend_contract(), shared)
    assert passed.passed, passed.logs

    (frontend / "index.html").write_text(
        '<button id="status-button"></button><script>fetch("/notes")</script>',
        encoding="utf-8",
    )
    failed = run_contract(str(frontend), _frontend_contract(), shared)
    assert not failed.passed
    assert any(f.repair_target == "worker" for f in failed.failures)


def test_route_contract_accepts_fastapi_callable_registration(tmp_path):
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "async def login(payload):\n"
        "    return {'message': 'ok'}\n"
        "app.post('/login')(login)\n",
        encoding="utf-8",
    )
    contract = ExecutionContract(
        module="backend",
        public_api=[{
            "id": "login-api",
            "method": "POST",
            "path": "/login",
            "response_200": {"message": "str"},
        }],
        constraints=[ContractCheck("files", "file", target={"allowed": ["main.py"]})],
    )

    report = run_contract(str(tmp_path), contract, implementation_language="python")

    assert report.passed, report.logs


def test_contract_rejects_frontend_python_signature_and_unknown_dom_fields():
    contract = ExecutionContract(
        module="frontend",
        internal=[
            ContractCheck(
                "bad-function", "function",
                target={"signature": "def request_health() -> None"},
            ),
            ContractCheck(
                "bad-dom", "dom",
                target={"elements": ["health-check-button"]},
            ),
        ],
        consumes=[{"id": "health", "provider": "backend", "method": "GET", "path": "/health"}],
        constraints=[ContractCheck("files", "file", target={"allowed": ["index.html"]})],
    )
    shared = {"backend": {"published_api": [{"method": "GET", "path": "/health"}]}}

    report = validate_execution_contract(
        contract, shared_contracts=shared, implementation_language="javascript")

    assert not report.passed
    assert all(f.repair_target == "planner" for f in report.failures)
    assert "Python signature" in report.logs
    assert "unsupported target fields" in report.logs


def test_forbidden_runtime_contract_fetch_is_a_worker_failure(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<button id="health-check-button"></button>'
        '<div id="health-status"></div>'
        '<script>fetch("/health"); fetch("contracts/backend_api.json")</script>',
        encoding="utf-8",
    )
    contract = ExecutionContract(
        module="frontend",
        internal=[
            ContractCheck("button", "dom", target={"ids": ["health-check-button", "health-status"]}),
            ContractCheck("health-fetch", "fetch", target={"path": "/health"}),
            ContractCheck(
                "no-contract-fetch", "fetch",
                target={"forbidden_paths": ["contracts/backend_api.json"]},
            ),
        ],
        consumes=[{"id": "health", "provider": "backend", "method": "GET", "path": "/health"}],
        constraints=[ContractCheck("files", "file", target={"allowed": ["index.html"]})],
    )
    shared = {"backend": {"published_api": [{"method": "GET", "path": "/health"}]}}

    report = run_contract(str(frontend), contract, shared)

    assert not report.passed
    assert any(f.check_id == "no-contract-fetch" and f.repair_target == "worker"
               for f in report.failures)


def test_available_api_allows_legacy_fetch_while_published_api_drives_new_consumes(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<button id="health-check-button"></button>'
        '<div id="health-status"></div>'
        '<script>fetch("/health"); fetch("/notes", {method: "POST"})</script>',
        encoding="utf-8",
    )
    contract = ExecutionContract(
        module="frontend",
        internal=[
            ContractCheck("button", "dom", target={"ids": ["health-check-button", "health-status"]}),
            ContractCheck("health-fetch", "fetch", target={"path": "/health"}),
        ],
        consumes=[{"id": "health", "provider": "backend", "method": "GET", "path": "/health"}],
        constraints=[ContractCheck("files", "file", target={"allowed": ["index.html"]})],
    )
    shared = {"backend": {
        "published_api": [{"method": "GET", "path": "/health"}],
        "available_api": [
            {"method": "GET", "path": "/health"},
            {"method": "POST", "path": "/notes"},
        ],
    }}

    report = run_contract(str(frontend), contract, shared, implementation_language="javascript")

    assert report.passed, report.logs


def test_test_engineer_validates_each_module_contract(tmp_path):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    contracts = tmp_path / "contracts"
    backend.mkdir()
    frontend.mkdir()
    contracts.mkdir()
    (backend / "main.py").write_text(
        'from fastapi import FastAPI\napp = FastAPI()\n@app.get("/status")\ndef status():\n    return {"status": "ok"}\n',
        encoding="utf-8",
    )
    (frontend / "index.html").write_text(
        '<button id="status-button"></button><script>fetch("/status")</script>',
        encoding="utf-8",
    )
    (backend / "contracts").mkdir()
    (frontend / "contracts").mkdir()
    backend_data = {
        "contracts": _backend_contract().public_api,
        "execution_contract": {
            "module": "backend",
            "public_api": _backend_contract().public_api,
            "internal": [],
            "consumes": [],
            "constraints": [{"id": "files", "kind": "file", "target": {"allowed": ["main.py"]}}],
        },
    }
    frontend_data = {
        "module": "frontend",
        "public_api": [],
        "internal": [{"id": "status-button", "kind": "dom", "target": {"ids": ["status-button"]}}],
        "consumes": [{"id": "backend-status", "provider": "backend", "method": "GET", "path": "/status", "allow_only": True}],
        "constraints": [{"id": "files", "kind": "file", "target": {"allowed": ["index.html"]}}],
    }
    (contracts / "backend_api.json").write_text(json.dumps(backend_data), encoding="utf-8")
    (backend / "contracts" / "execution_contract.json").write_text(
        json.dumps(backend_data["execution_contract"]), encoding="utf-8")
    (frontend / "contracts" / "execution_contract.json").write_text(json.dumps(frontend_data), encoding="utf-8")

    ok, log, failures, repair_modules = _run_shared_contract_check(
        str(contracts), str(backend), str(frontend),
        {"backend": backend_data}, ["backend", "frontend"],
    )
    assert ok, log
    assert not failures
    assert not repair_modules


def test_usage_limit_text_is_a_hard_failure():
    result = AgentResult(text="You've hit your usage limit", ok=True)
    assert is_usage_limit_result(result)
    assert is_usage_limit_result(AgentResult(text="API Error: 402 Insufficient Balance", ok=False))


def test_usage_limit_summary_keeps_error_lines_not_prompt_tail():
    result = _normalize_result(AgentResult(
        text='请写入 ".arbiter_plan.json"作为确认，不要输出别的内容。\n'
             '需求：做一个网页\n'
             'ERROR: You\'ve hit your usage limit. try again later.',
        ok=True,
    ))

    assert not result.ok
    assert result.failure_kind == "usage_limit"
    assert "ERROR: You've hit your usage limit" in result.error
    assert "需求：做一个网页" not in result.error


def test_provider_api_error_text_is_not_reported_ok():
    result = _normalize_result(AgentResult(text="API Error: 529 Overloaded", ok=True))

    assert not result.ok
    assert result.failure_kind == "provider"


def test_provider_error_detection_does_not_flag_normal_numbers():
    plan_text = "frontend_url http://localhost:3000; max-width: 400px"

    assert not _looks_like_api_failure(plan_text)
    assert not _looks_like_auth_failure(plan_text)
    assert _looks_like_api_failure("API Error: 401 Unauthorized")
    assert _looks_like_auth_failure("Authentication Fails: invalid api key")


def test_registry_keeps_low_levels_on_deepseek_glm_and_high_levels_on_codex():
    assert resolve(1).provider == "deepseek"
    assert resolve(2).provider == "glm"
    assert resolve(3).provider == "deepseek"
    assert resolve(4).provider == "codex"
    assert resolve(5).provider == "codex"
    assert resolve(6).provider == "codex"


def test_low_level_provider_failure_does_not_fallback_to_codex():
    assert fallback_level_for_provider_failure(2, "glm", "GLM_API_KEY 未设置") == 3
    assert fallback_level_for_provider_failure(1, "deepseek", "API Error: 529 Overloaded") == 3
    assert fallback_level_for_provider_failure(3, "deepseek", "DEEPSEEK_API_KEY 未设置") is None
    assert fallback_level_for_provider_failure(4, "codex", "usage limit") is None


def test_configured_smart_level_reads_role_env_without_fallback_logic(monkeypatch):
    monkeypatch.setenv("GG_MODULE_PLANNER_LEVEL", "3")
    assert configured_smart_level("MODULE_PLANNER", 5) == 3

    monkeypatch.setenv("GG_MODULE_PLANNER_LEVEL", "codex")
    assert configured_smart_level("MODULE_PLANNER", 5) == 5

    monkeypatch.setenv("GG_MODULE_PLANNER_LEVEL", "9")
    assert configured_smart_level("MODULE_PLANNER", 5) == 5


def test_playwright_plan_urls_are_rewritten_to_runtime_ports():
    class Standard:
        backend_port = 18000
        frontend_port = 13000

    assert _plan_url("http://localhost:3000", Standard) == "http://127.0.0.1:13000"
    assert _plan_url("http://localhost:8000/greet", Standard) == "http://127.0.0.1:18000/greet"


def test_extract_plan_json_prefers_fenced_test_cases():
    text = (
        '分析：请求体 {"username": "str"}。\n'
        '```json\n'
        '{"backend_base_url":"http://localhost:8000","test_cases":[{"id":"tc-1","steps":[]}]}'
        '\n```\n'
    )

    extracted = _extract_plan_json(text)

    assert extracted is not None
    assert extracted["test_cases"][0]["id"] == "tc-1"


def test_frontend_only_e2e_does_not_require_backend_service():
    assert not _requires_backend_service(["frontend"], [])
    assert _requires_backend_service(["backend", "frontend"], [])
    assert _requires_backend_service(["frontend"], ["/score"])


def _free_port() -> int:
    import socket
    import pytest

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
    except PermissionError:
        pytest.skip("socket creation is not permitted in this sandbox")


def test_service_lifecycle_skips_backend_for_frontend_only(tmp_path):
    from types import SimpleNamespace

    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (frontend / "index.html").write_text("<button>Hello</button>", encoding="utf-8")
    standard = SimpleNamespace(
        backend_port=_free_port(),
        frontend_port=_free_port(),
        startup_timeout=5,
    )

    be_proc = fe_proc = None
    try:
        be_proc, fe_proc, log = asyncio.run(_start_services(
            str(backend), str(frontend), standard, [], False))
        assert be_proc is None
        assert fe_proc is not None
        assert "backend pid=(skipped)" in log
    finally:
        asyncio.run(_stop_process(be_proc))
        asyncio.run(_stop_process(fe_proc))


def test_service_lifecycle_starts_backend_when_api_is_declared(tmp_path):
    from types import SimpleNamespace

    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    (backend / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (frontend / "index.html").write_text("<script>fetch('/health')</script>", encoding="utf-8")
    standard = SimpleNamespace(
        backend_port=_free_port(),
        frontend_port=_free_port(),
        startup_timeout=5,
    )

    be_proc = fe_proc = None
    try:
        be_proc, fe_proc, log = asyncio.run(_start_services(
            str(backend), str(frontend), standard, ["/health"], True))
        assert be_proc is not None
        assert fe_proc is not None
        assert f"port={standard.backend_port}" in log
    finally:
        asyncio.run(_stop_process(be_proc))
        asyncio.run(_stop_process(fe_proc))


def test_run_mcp_tests_executes_contract_and_playwright_login_flow(tmp_path):
    from types import SimpleNamespace
    from test_engineer1.standard import E2ETestStandard

    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    contracts = tmp_path / "contracts"
    tester = tmp_path / "tester"
    for path in (backend, frontend, contracts, tester):
        path.mkdir()

    (backend / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "from pydantic import BaseModel\n"
        "app = FastAPI()\n"
        "LOGIN_RECORDS = []\n"
        "class LoginPayload(BaseModel):\n"
        "    username: str\n"
        "    password: str\n"
        "@app.post('/login')\n"
        "def login(payload: LoginPayload):\n"
        "    LOGIN_RECORDS.append(payload.username)\n"
        "    return {'ok': True, 'message': f'Welcome {payload.username}'}\n",
        encoding="utf-8",
    )
    (frontend / "index.html").write_text(
        "<!doctype html><html><body>"
        "<form id='login-form'>"
        "<input id='username' name='username'>"
        "<input id='password' name='password' type='password'>"
        "<button id='login-button' type='submit'>Login</button>"
        "</form>"
        "<div id='login-message'></div>"
        "<script>"
        "document.getElementById('login-form').addEventListener('submit', async (event) => {"
        "event.preventDefault();"
        "const username = document.getElementById('username').value;"
        "const password = document.getElementById('password').value;"
        "const res = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username, password})});"
        "const data = await res.json();"
        "document.getElementById('login-message').textContent = data.message;"
        "});"
        "</script></body></html>",
        encoding="utf-8",
    )

    backend_contract = ExecutionContract(
        module="backend",
        public_api=[{
            "id": "login",
            "method": "POST",
            "path": "/login",
            "request": {"username": "str", "password": "str"},
            "response_200": {"ok": "bool", "message": "str"},
        }],
        constraints=[ContractCheck("files", "file", target={"allowed": ["main.py"]})],
    )
    frontend_contract = ExecutionContract(
        module="frontend",
        internal=[
            ContractCheck("login-dom", "dom", target={"ids": [
                "login-form", "username", "password", "login-button", "login-message",
            ]}),
            ContractCheck("login-fetch", "fetch", target={
                "path": "/login",
                "fields": {"username": "str", "password": "str"},
            }),
        ],
        consumes=[{
            "id": "backend-login",
            "provider": "backend",
            "method": "POST",
            "path": "/login",
            "allow_only": True,
        }],
        constraints=[ContractCheck("files", "file", target={"allowed": ["index.html"]})],
    )
    backend_payload = {
        "published_api": backend_contract.public_api,
        "available_api": backend_contract.public_api,
        "execution_contract": execution_contract_to_dict(backend_contract),
    }
    (contracts / "backend_api.json").write_text(json.dumps(backend_payload), encoding="utf-8")
    (backend / "contracts").mkdir()
    (frontend / "contracts").mkdir()
    (backend / "contracts" / "execution_contract.json").write_text(
        json.dumps(execution_contract_to_dict(backend_contract)),
        encoding="utf-8",
    )
    (frontend / "contracts" / "execution_contract.json").write_text(
        json.dumps(execution_contract_to_dict(frontend_contract)),
        encoding="utf-8",
    )

    standard = E2ETestStandard(
        required=frozenset({"api_contract", "playwright"}),
        backend_port=_free_port(),
        frontend_port=_free_port(),
        startup_timeout=5,
        test_timeout=15,
    )
    plan = {
        "test_cases": [{
            "id": "login-success",
            "title": "提交登录表单后显示后端返回信息",
            "steps": [
                {"action": "navigate", "url": "http://localhost:3000"},
                {"action": "fill", "selector": "#username", "value": "alice"},
                {"action": "fill", "selector": "#password", "value": "secret"},
                {"action": "click", "selector": "#login-button"},
                {"action": "expect_text", "selector": "#login-message", "contains": "Welcome alice"},
            ],
        }],
    }
    cfg = SimpleNamespace(
        backend_workspace=str(backend),
        frontend_workspace=str(frontend),
        contract_workspace=str(contracts),
        test_workspace=str(tester),
        human_standard=standard,
        log_path="",
    )

    result = asyncio.run(run_mcp_tests({
        "test_plan": json.dumps(plan),
        "backend_workspace": str(backend),
        "frontend_workspace": str(frontend),
        "contract_workspace": str(contracts),
        "shared_contracts": {"backend": backend_payload},
        "active_modules": ["backend", "frontend"],
        "standard": standard,
    }, cfg))

    assert result["passed"], result["run_logs"]
    assert result["phase"] == "playwright"
    assert any(path.endswith("contract_check.log") for path in result["artifacts"])
    assert any(path.endswith("service_startup.log") for path in result["artifacts"])
    assert any(path.endswith("playwright_mcp.log") for path in result["artifacts"])


def test_batch_arbiter_stops_when_decision_model_hits_quota(monkeypatch):
    import arbiter

    async def quota_result(**_kwargs):
        return AgentResult(text="ERROR: You've hit your usage limit", ok=True)

    monkeypatch.setattr(arbiter, "run_agent", quota_result)
    decision, reason = asyncio.run(arbiter._ai_batch_decision(0, [], [], []))

    assert decision == "HUMAN_STOP"
    assert "额度/会话限制" in reason


def test_arbiter_intake_stops_on_quota_without_tasks(tmp_path, monkeypatch):
    import arbiter
    import programmer1.devlog as devlog_module

    async def quota_result(**_kwargs):
        return AgentResult(text="ERROR: You've hit your usage limit", ok=True)

    monkeypatch.setattr(arbiter, "run_agent", quota_result)
    monkeypatch.setattr(arbiter, "ARBITER_WORKSPACE", tmp_path / "arbiter")
    monkeypatch.setattr(arbiter, "_PLAN_FILE", tmp_path / "arbiter" / ".arbiter_plan.json")
    monkeypatch.setattr(devlog_module, "_DEVLOG_PATH", tmp_path / "devlog.md")

    result = asyncio.run(arbiter.arbiter_intake({
        "requirement": "做一个登录页面",
        "dev_mode": True,
    }))

    assert result["decision"] == "escalate"
    assert "额度/会话限制" in result["result"]
    assert "task_batches" not in result
    assert not (tmp_path / "arbiter" / ".arbiter_plan.json").exists()


def test_devlog_switches_between_full_output_and_receipts(tmp_path, monkeypatch):
    import programmer1.devlog as devlog_module

    monkeypatch.setattr(devlog_module, "_DEVLOG_PATH", tmp_path / "devlog.md")
    detailed = devlog_module.DevlogSession()
    detailed.start("detailed", detailed=True)
    detailed.log_model_call(
        trace_name="tester", provider="codex", model="x", level=5,
        elapsed_seconds=0.1, ok=True, failure_kind="", output="complete raw model output",
    )
    detailed.save()
    assert "complete raw model output" in (tmp_path / "devlog.md").read_text()

    simple = devlog_module.DevlogSession()
    simple.start("simple", detailed=False)
    simple.log_model_call(
        trace_name="tester", provider="codex", model="x", level=5,
        elapsed_seconds=0.1, ok=True, failure_kind="", output="should not be retained",
    )
    simple.save()
    saved = (tmp_path / "devlog.md").read_text()
    assert "model `tester`" in saved
    assert "should not be retained" not in saved


def test_contract_failure_restarts_from_the_affected_batch():
    backend_task = Task(id="be", title="backend", owner="backend")
    frontend_task = Task(id="fe", title="frontend", owner="frontend")
    state = {
        "task_batches": [[backend_task], [frontend_task]],
        "test_retries": 0,
        "test_result": WorkflowTestResult(
            passed=False, phase="api_contract", failures=["backend route missing"],
            repair_modules=["backend"],
        ),
    }

    result = arbiter_review_test(state)

    assert result["decision"] == "rollback"
    assert result["batch_cursor"] == 0
    assert result["module_tasks"] == [backend_task]
    assert result["task_batches"] == [[backend_task], [frontend_task]]


def test_retry_exhausted_module_report_forces_human_stop():
    import arbiter

    report = arbiter.ModuleReport(
        module="frontend",
        ok=False,
        summary="retry exhausted after 2 attempts",
        note="retry exhausted after 2 attempts: contract still failing",
    )

    assert arbiter._requires_human_stop(report)


def test_reviewer_retry_on_system_contract_files_and_invented_line_limit_is_ignored():
    assert _review_retry_is_only_ignored_noise(
        "retry_worker",
        "实际改动包含 files_allowed 外的 contracts 文件，且 index.html 超过 500 行限制。",
        [
            "files_allowed 仅允许 index.html，但工作树新增了 contracts/contract_report.json 和 contracts/execution_contract.json。",
            "index.html 为 733 行，超过 500 行限制。",
        ],
    )

    assert not _review_retry_is_only_ignored_noise(
        "retry_worker",
        "开始按钮没有启动游戏循环。",
        ["点击 start-button 后 running 未变为 true。"],
    )


def test_workspace_baseline_preserves_existing_files_and_reports_only_new_changes(tmp_path):
    (tmp_path / "index.html").write_text("before", encoding="utf-8")
    baseline = _workspace_snapshot(str(tmp_path))

    (tmp_path / "index.html").write_text("after", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("new", encoding="utf-8")

    assert _changed_files(str(tmp_path), baseline=baseline) == ["index.html", "notes.txt"]


def test_review_allowed_files_include_execution_contract_constraints():
    task = Task(id="api", title="api", files_allowed=["main.py"])
    contract = ExecutionContract(
        module="backend",
        public_api=[{"id": "login", "method": "POST", "path": "/login"}],
        constraints=[ContractCheck("files", "file", target={"allowed": ["main.py", "requirements.txt"]})],
    )

    assert _allowed_files_for_review([task], contract, "backend") == {"main.py", "requirements.txt"}


def test_contract_proxy_only_routes_declared_api_paths():
    api_paths = {"/health", "/notes"}

    assert should_proxy_path("/health", api_paths)
    assert should_proxy_path("/notes?limit=10", api_paths)
    assert not should_proxy_path("/index.html", api_paths)
    assert not should_proxy_path("/contracts/backend_api.json", api_paths)


def test_route_patterns_cover_original_normalized_and_path_glob():
    standard = SimpleNamespace(backend_port=8123, frontend_port=3456)

    assert _route_patterns("http://localhost:8000/login", standard) == [
        "http://localhost:8000/login",
        "http://127.0.0.1:8123/login",
        "**/login",
    ]


def test_url_matches_supports_playwright_glob_suffix():
    assert _url_matches("**/login", "http://localhost:8000/login")
    assert _url_matches("contracts/backend_api.json", "http://localhost:3000/contracts/backend_api.json")
    assert not _url_matches("**/login", "http://localhost:3000/index.html")


def test_consumer_pact_verification_checks_provider_published_api(tmp_path):
    contracts = tmp_path / "contracts"
    consumer_dir = contracts / "consumers"
    consumer_dir.mkdir(parents=True)
    pact = {
        "consumer_module": "frontend",
        "provider_module": "backend",
        "interactions": [{
            "id": "login",
            "provider": "backend",
            "method": "POST",
            "path": "/login",
        }],
    }
    (consumer_dir / "frontend__backend.json").write_text(json.dumps(pact), encoding="utf-8")

    pacts = _consumer_pacts(str(contracts), {})
    failures, repair_modules = _verify_consumer_pacts(
        {"backend": {"published_api": [{"method": "POST", "path": "/login"}]}},
        pacts,
    )
    assert failures == []
    assert repair_modules == []

    failures, repair_modules = _verify_consumer_pacts(
        {"backend": {"published_api": [{"method": "GET", "path": "/health"}]}},
        pacts,
    )
    assert "does not satisfy consumer pact" in failures[0]
    assert repair_modules == ["backend"]


def test_test_plan_validator_rejects_invalid_runner_dsl():
    errors = validate_test_plan({
        "test_cases": [{
            "id": "bad",
            "steps": [
                {"action": "navigate", "url": "http://localhost:3000"},
                {"action": "expect_request", "method": "POST"},
                {"action": "route_intercept", "path": "/login"},
                {"action": "click", "selector": "#login-button"},
            ],
        }],
    })

    assert any("request path/url is required" in error for error in errors)
    assert any("expect_request must follow" in error for error in errors)
    assert any("route_intercept must declare" in error for error in errors)
