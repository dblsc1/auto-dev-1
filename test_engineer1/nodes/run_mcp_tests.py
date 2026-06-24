"""test_engineer1/nodes/run_mcp_tests.py — Python service lifecycle + Playwright E2E.

替代原来的 write_tests + run_tests 两个节点：
  · Python 负责起/停 FastAPI 与同源静态代理
  · Codex 只负责生成测试计划 JSON
  · Python Playwright 直接执行计划，不再调用外部浏览器 agent
"""
from __future__ import annotations

import json
import asyncio
import sys
from pathlib import Path
from typing import Any

from ..state import TEState
from contract_cfg import BACKEND_CONTRACT_FILE, CONSUMER_CONTRACT_DIR
from programmer1.contracts import (
    ExecutionContract,
    execution_contract_from_dict,
    execution_contract_to_dict,
    run_contract,
)

def _load_json(path: Path) -> tuple[dict | None, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"未找到文件：{path}"
    except Exception as exc:
        return None, f"JSON 解析失败：{path} — {exc}"
    if not isinstance(data, dict):
        return None, f"JSON 顶层必须是对象：{path}"
    return data, ""


def _load_module_contract(workspace: str, module: str) -> tuple[ExecutionContract | None, str]:
    path = Path(workspace) / "contracts" / "execution_contract.json"
    data, error = _load_json(path)
    if data is None:
        return None, error
    try:
        return execution_contract_from_dict(data, module=module), ""
    except Exception as exc:
        return None, f"execution_contract 无效：{path} — {exc}"


def _backend_payload(contract_workspace: str, shared_contracts: dict) -> tuple[dict | None, str]:
    payload = shared_contracts.get("backend") if isinstance(shared_contracts, dict) else None
    if isinstance(payload, dict) and payload:
        return payload, ""
    return _load_json(Path(contract_workspace) / BACKEND_CONTRACT_FILE)


def _published_api(payload: dict) -> list[dict]:
    return list(
        payload.get("published_api")
        or payload.get("public_api")
        or payload.get("contracts")
        or []
    )


def _available_api(payload: dict) -> list[dict]:
    return list(
        payload.get("available_api")
        or payload.get("published_api")
        or payload.get("public_api")
        or payload.get("contracts")
        or []
    )


def _consumer_pacts(contract_workspace: str, shared_contracts: dict) -> list[dict]:
    pacts: list[dict] = []
    consumers = shared_contracts.get("consumers") if isinstance(shared_contracts, dict) else {}
    if isinstance(consumers, dict):
        for provider_map in consumers.values():
            if isinstance(provider_map, dict):
                for pact in provider_map.values():
                    if isinstance(pact, dict):
                        pacts.append(pact)

    consumer_dir = Path(contract_workspace) / CONSUMER_CONTRACT_DIR
    if consumer_dir.exists():
        for path in sorted(consumer_dir.glob("*.json")):
            data, _error = _load_json(path)
            if isinstance(data, dict):
                pacts.append(data)

    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for pact in pacts:
        key = (
            str(pact.get("consumer_module") or ""),
            str(pact.get("provider_module") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pact)
    return deduped


def _verify_consumer_pacts(shared_contracts: dict, pacts: list[dict]) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    repair_modules: set[str] = set()
    for pact in pacts:
        provider = str(pact.get("provider_module") or "")
        consumer = str(pact.get("consumer_module") or "consumer")
        provider_contract = shared_contracts.get(provider) if isinstance(shared_contracts, dict) else {}
        provider_api = _published_api(provider_contract or {})
        provider_keys = {
            (str(item.get("method") or "").upper(), str(item.get("path") or ""))
            for item in provider_api
        }
        for interaction in pact.get("interactions") or []:
            method = str(interaction.get("method") or "").upper()
            path = str(interaction.get("path") or "")
            if not provider or not method or not path:
                failures.append(
                    f"consumer pact {consumer}->{provider or '?'} has invalid interaction {interaction!r}"
                )
                repair_modules.add(consumer)
                continue
            if (method, path) not in provider_keys:
                failures.append(
                    f"provider {provider} does not satisfy consumer pact "
                    f"{consumer}: {method} {path}"
                )
                repair_modules.add(provider)
    return failures, sorted(repair_modules)


def _format_contract_failures(report) -> list[str]:
    return [
        f"{failure.check_id} [{failure.kind}] repair_target={failure.repair_target}: {failure.reason}"
        for failure in report.failures
    ]


def _run_shared_contract_check(
    contract_workspace: str,
    backend_workspace: str,
    frontend_workspace: str,
    shared_contracts: dict,
    active_modules: list[str],
) -> tuple[bool, str, list[str], list[str]]:
    """Check each module's own contract, then the shared backend API dependency."""
    logs: list[str] = []
    failures: list[str] = []
    repair_modules: set[str] = set()
    active = set(active_modules)
    backend_payload, payload_error = _backend_payload(contract_workspace, shared_contracts)
    normalized_shared = dict(shared_contracts or {})

    if "backend" in active:
        if backend_payload is None:
            return False, payload_error, [payload_error], ["backend"]
        raw_backend = backend_payload.get("execution_contract")
        if not isinstance(raw_backend, dict) or not raw_backend:
            message = "backend_api.json 缺少 backend execution_contract"
            return False, message, [message], ["backend"]
        backend_contract, error = _load_module_contract(backend_workspace, "backend")
        if backend_contract is None:
            logs.append("[backend/definition]\n" + error)
            failures.append(error)
            repair_modules.add("backend")
        else:
            shared_backend_contract = execution_contract_from_dict(raw_backend, module="backend")
            if execution_contract_to_dict(backend_contract) != execution_contract_to_dict(shared_backend_contract):
                failures.append("backend module execution_contract 与 shared backend_api.json 不一致")
                logs.append("[backend/definition]\n❌ module/shared execution_contract mismatch")
                repair_modules.add("backend")
            backend_report = run_contract(
                backend_workspace,
                backend_contract,
                implementation_language="python",
            )
            logs.append("[backend/run_contract]\n" + backend_report.logs)
            failures.extend(_format_contract_failures(backend_report))
            if not backend_report.passed:
                repair_modules.add("backend")
        normalized_shared["backend"] = backend_payload

    if "frontend" in active:
        frontend_contract, error = _load_module_contract(frontend_workspace, "frontend")
        if frontend_contract is None:
            logs.append("[frontend/definition]\n" + error)
            failures.append(error)
            repair_modules.add("frontend")
        else:
            frontend_report = run_contract(
                frontend_workspace,
                frontend_contract,
                shared_contracts=normalized_shared,
                implementation_language="javascript",
            )
            logs.append("[frontend/run_contract]\n" + frontend_report.logs)
            failures.extend(_format_contract_failures(frontend_report))
            if not frontend_report.passed:
                repair_modules.add("frontend")

    if not active:
        return True, "（没有参与 E2E 的编码模块，跳过契约检查）", [], []

    logs.insert(0, f"共享 API 契约目录：{Path(contract_workspace) / BACKEND_CONTRACT_FILE}")
    consumer_pacts = _consumer_pacts(contract_workspace, normalized_shared)
    pact_failures, pact_repair_modules = _verify_consumer_pacts(normalized_shared, consumer_pacts)
    if consumer_pacts:
        logs.append(
            "[consumer-pacts]\n"
            + ("\n".join(f"✅ {p.get('consumer_module')} -> {p.get('provider_module')}"
                         for p in consumer_pacts)
               if not pact_failures else "\n".join(f"❌ {f}" for f in pact_failures))
        )
    failures.extend(pact_failures)
    repair_modules.update(pact_repair_modules)
    return (not failures, "\n\n".join(logs), list(dict.fromkeys(failures)),
            sorted(repair_modules))


def _persist_log(cfg, filename: str, text: str) -> str:
    if cfg.log_path:
        configured = Path(cfg.log_path)
        path = (
            configured.with_name(f"{configured.stem}_{filename}")
            if configured.suffix else configured / filename
        )
    else:
        path = Path(cfg.test_workspace) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


async def _wait_port(port: int, timeout: int) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    last_error = ""
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError as exc:
            last_error = str(exc)
            await asyncio.sleep(0.25)
    raise TimeoutError(f"port {port} not ready after {timeout}s: {last_error}")


async def _stop_process(proc) -> None:
    if not proc or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass


async def _process_output(proc) -> str:
    if not proc or not proc.stdout:
        return ""
    try:
        chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.2)
        return chunk.decode(errors="replace") if chunk else ""
    except Exception:
        return ""


async def _start_services(
    be_ws: str,
    fe_ws: str,
    standard,
    api_paths: list[str],
    backend_required: bool,
) -> tuple[object, object, str]:
    backend_log: list[str] = []
    frontend_log: list[str] = []
    be_proc = fe_proc = None
    try:
        if backend_required:
            be_proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "uvicorn", "main:app",
                "--host", "127.0.0.1", "--port", str(standard.backend_port),
                cwd=be_ws,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        project_root = Path(__file__).resolve().parents[2]
        proxy_args = [
            sys.executable, "-m", "test_engineer1.proxy_server",
            "--directory", str(Path(fe_ws).resolve()),
            "--port", str(standard.frontend_port),
            "--backend-url", f"http://127.0.0.1:{standard.backend_port}",
        ]
        for path in api_paths:
            proxy_args.extend(["--api-path", path])
        fe_proc = await asyncio.create_subprocess_exec(
            *proxy_args,
            cwd=project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await asyncio.sleep(0.1)
        failed = [
            ("backend", be_proc) if be_proc and be_proc.returncode is not None else None,
            ("frontend proxy", fe_proc) if fe_proc.returncode is not None else None,
        ]
        failed = [item for item in failed if item is not None]
        if failed:
            details = []
            for label, proc in failed:
                details.append(f"{label} exited early ({proc.returncode}): {await _process_output(proc)}")
            raise RuntimeError("\n".join(details))
        if backend_required:
            await _wait_port(standard.backend_port, standard.startup_timeout)
        await _wait_port(standard.frontend_port, standard.startup_timeout)
        return be_proc, fe_proc, (
            f"backend pid={be_proc.pid if be_proc else '(skipped)'} port={standard.backend_port}\n"
            f"frontend pid={fe_proc.pid} port={standard.frontend_port}"
        )
    except Exception:
        for proc, logs in ((be_proc, backend_log), (fe_proc, frontend_log)):
            output = await _process_output(proc)
            if output:
                logs.append(output)
        await _stop_process(be_proc)
        await _stop_process(fe_proc)
        detail = "\n".join([
            "backend log:",
            "".join(backend_log)[-2000:],
            "frontend log:",
            "".join(frontend_log)[-2000:],
        ])
        raise RuntimeError(detail)


def _chromium_launch_kwargs() -> dict[str, str]:
    import os
    import shutil
    import glob

    cache = os.path.expanduser("~/.cache/ms-playwright")
    shells = glob.glob(f"{cache}/chromium*/**/chrome-headless-shell", recursive=True)
    bins = glob.glob(f"{cache}/chromium*/**/chrome", recursive=True)
    if shells or bins:
        return {}
    for candidate in (
        "/snap/bin/chromium",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        shutil.which("chromium") or "",
    ):
        if candidate and os.path.exists(candidate):
            return {"executable_path": candidate}
    return {}


def _plan_url(url: str, standard) -> str:
    if not url:
        return f"http://127.0.0.1:{standard.frontend_port}"
    return (
        url.replace("http://localhost:3000", f"http://127.0.0.1:{standard.frontend_port}")
           .replace("http://127.0.0.1:3000", f"http://127.0.0.1:{standard.frontend_port}")
           .replace("http://localhost:8000", f"http://127.0.0.1:{standard.backend_port}")
           .replace("http://127.0.0.1:8000", f"http://127.0.0.1:{standard.backend_port}")
    )


def _requires_backend_service(active_modules: list[str], api_paths: list[str]) -> bool:
    return "backend" in set(active_modules) or bool(api_paths)


async def _expect_text(page, selector: str, step: dict[str, Any]) -> None:
    timeout_ms = int(step.get("timeout_ms") or 5000)
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    exact_key = "equals" if "equals" in step else "exact" if "exact" in step else ""
    expected = str(step.get(exact_key) if exact_key else step.get("contains", ""))
    last_text = ""
    while True:
        try:
            if selector == "title":
                last_text = await page.title()
            else:
                last_text = await page.locator(selector).inner_text(timeout=500)
            if exact_key and last_text.strip() == expected:
                return
            if not exact_key and expected in last_text:
                return
        except Exception as exc:
            last_text = f"<{type(exc).__name__}: {exc}>"
        if asyncio.get_event_loop().time() >= deadline:
            raise AssertionError(
                f"{selector} text {last_text!r} does not match {expected!r}"
            )
        await page.wait_for_timeout(100)


def _url_path(pattern: str) -> str:
    if "://" not in pattern:
        return pattern if pattern.startswith("/") else ""
    rest = pattern.split("://", 1)[1]
    slash = rest.find("/")
    if slash < 0:
        return ""
    return rest[slash:].split("?", 1)[0]


def _route_patterns(pattern: str, standard) -> list[str]:
    raw = pattern or "**/*"
    normalized = _plan_url(raw, standard)
    variants: list[str] = []
    for candidate in (raw, normalized):
        if candidate and candidate not in variants:
            variants.append(candidate)
    path = _url_path(raw) or _url_path(normalized)
    if path:
        glob = f"**{path}"
        if glob not in variants:
            variants.append(glob)
    return variants


def _url_matches(pattern: str, url: str) -> bool:
    if not pattern or pattern == "**/*":
        return True
    if pattern.startswith("**"):
        return pattern[2:] in url
    return pattern in url


async def _expect_attribute(page, selector: str, step: dict[str, Any], *, present_default: bool = True) -> None:
    attr_name = str(step.get("attribute") or step.get("attr") or step.get("name") or "")
    if not attr_name:
        raise AssertionError(f"{selector} attribute check missing attribute/name")
    attr = await page.locator(str(selector)).get_attribute(attr_name)
    if "contains" in step:
        expected = str(step.get("contains") or "")
        if expected not in str(attr or ""):
            raise AssertionError(f"{selector} attribute {attr_name}={attr!r} does not contain {expected!r}")
        return
    if "exact" in step or "equals" in step or "value" in step:
        expected = str(step.get("exact") if "exact" in step else step.get("equals", step.get("value", "")))
        if attr is None:
            raise AssertionError(f"{selector} missing attribute {attr_name}")
        if str(attr or "") != expected:
            raise AssertionError(f"{selector} attribute {attr_name}={attr!r} != {expected!r}")
        return
    present = bool(step.get("present", present_default))
    if present and attr is None:
        raise AssertionError(f"{selector} missing attribute {attr_name}")
    if not present and attr is not None:
        raise AssertionError(f"{selector} unexpectedly has attribute {attr_name}")


async def _run_playwright_plan(plan: dict[str, Any], standard) -> tuple[bool, str, list[str]]:
    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return False, f"Playwright import failed: {exc}", [f"Playwright import failed: {exc}"]

    logs: list[str] = []
    failures: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, **_chromium_launch_kwargs())
        page = await browser.new_page()
        captured_requests: list[dict[str, str]] = []
        route_patterns: list[str] = []
        page.on("request", lambda request: captured_requests.append({
            "url": request.url,
            "method": request.method,
            "body": request.post_data or "",
        }))
        try:
            for case in plan.get("test_cases", []):
                case_id = str(case.get("id") or "unnamed")
                logs.append(f"[{case_id}] {case.get('title', '')}")
                try:
                    for step in case.get("steps", []):
                        action = str(step.get("action") or "")
                        selector = step.get("selector")
                        if action == "navigate":
                            await page.goto(_plan_url(str(step.get("url") or ""), standard))
                        elif action in {"start_request_capture", "setup_network_capture"}:
                            captured_requests.clear()
                        elif action == "fill":
                            await page.fill(str(selector), str(step.get("value", "")))
                        elif action == "click":
                            await page.click(str(selector))
                        elif action == "press":
                            await page.keyboard.press(str(step.get("key") or ""))
                        elif action in {"wait", "wait_for_timeout"}:
                            await page.wait_for_timeout(int(step.get("ms") or step.get("value") or 0))
                        elif action in {"route_intercept", "route"}:
                            patterns = _route_patterns(
                                str(step.get("url_pattern") or step.get("url") or step.get("path") or "**/*"),
                                standard,
                            )
                            method = str(step.get("method") or "").upper()
                            status = int(step.get("status") or 0)
                            body = str(step.get("body") or "")
                            strategy = str(
                                step.get("strategy")
                                or step.get("route_action")
                                or step.get("mode")
                                or ""
                            )
                            delay_ms = int(step.get("delay_ms") or step.get("delay") or 0)
                            if delay_ms and not strategy and not status and not body:
                                strategy = "delay"
                            content_type = str(step.get("content_type") or "application/json")

                            async def _handler(
                                route, request, method=method, status=status, body=body,
                                strategy=strategy, delay_ms=delay_ms, content_type=content_type,
                            ):
                                if method and request.method.upper() != method:
                                    await route.continue_()
                                    return
                                if delay_ms:
                                    await asyncio.sleep(delay_ms / 1000)
                                if strategy == "delay" and not status:
                                    await route.continue_()
                                    return
                                if strategy == "abort":
                                    await route.abort()
                                    return
                                if status:
                                    await route.fulfill(status=status, body=body, content_type=content_type)
                                else:
                                    await route.abort()

                            for pattern in patterns:
                                await page.route(pattern, _handler)
                                route_patterns.append(pattern)
                        elif action == "route_clear":
                            for pattern in route_patterns:
                                await page.unroute(pattern)
                            route_patterns.clear()
                        elif action == "expect_visible":
                            await page.wait_for_selector(str(selector), state="visible", timeout=5000)
                        elif action in {"expect_present", "expect_exists", "expect_attached"}:
                            await page.wait_for_selector(str(selector), state="attached", timeout=5000)
                        elif action == "expect_not_visible":
                            await page.wait_for_selector(str(selector), state="hidden", timeout=5000)
                        elif action == "expect_text":
                            await _expect_text(page, str(selector), step)
                        elif action == "expect_class":
                            expected_class = str(step.get("class") or step.get("contains") or "")
                            class_attr = await page.locator(str(selector)).get_attribute("class")
                            classes = set((class_attr or "").split())
                            if expected_class not in classes:
                                raise AssertionError(
                                    f"{selector} classes {sorted(classes)!r} missing {expected_class!r}"
                                )
                        elif action == "expect_computed_style":
                            prop = str(step.get("property") or "")
                            expected = str(step.get("value") or "")
                            actual = await page.locator(str(selector)).evaluate(
                                "(el, prop) => getComputedStyle(el).getPropertyValue(prop)",
                                prop,
                            )
                            if str(actual).strip() != expected:
                                raise AssertionError(
                                    f"{selector} computed {prop}={actual!r} != {expected!r}"
                                )
                        elif action == "expect_count":
                            count = await page.locator(str(selector)).count()
                            if "equals" in step and count != int(step["equals"]):
                                raise AssertionError(f"{selector} count {count} != {step['equals']}")
                            if "greater_than" in step and count <= int(step["greater_than"]):
                                raise AssertionError(f"{selector} count {count} <= {step['greater_than']}")
                        elif action == "expect_attribute":
                            await _expect_attribute(page, str(selector), step)
                        elif action == "expect_not_attribute":
                            await _expect_attribute(page, str(selector), step, present_default=False)
                        elif action in {"expect_no_request", "expect_no_request_to", "assert_no_request_to"}:
                            needle = str(
                                step.get("path_contains")
                                or step.get("url_contains")
                                or step.get("url_pattern")
                                or step.get("url")
                                or step.get("path")
                                or ""
                            )
                            if any(_url_matches(needle, req["url"]) for req in captured_requests):
                                raise AssertionError(f"unexpected request containing {needle!r}")
                        elif action == "expect_no_request_to_path":
                            needle = str(
                                step.get("path_contains")
                                or step.get("url_contains")
                                or step.get("url_pattern")
                                or step.get("url")
                                or ""
                            )
                            if any(_url_matches(needle, req["url"]) for req in captured_requests):
                                raise AssertionError(f"unexpected request path containing {needle!r}")
                        elif action == "expect_request":
                            method = str(step.get("method") or "").upper()
                            path = str(
                                step.get("path_contains")
                                or step.get("url_contains")
                                or step.get("url_pattern")
                                or step.get("url")
                                or step.get("path")
                                or ""
                            )
                            body_value = (
                                step.get("body_contains_json")
                                or step.get("body_contains")
                                or step.get("body")
                                or ""
                            )
                            body = (
                                json.dumps(body_value, ensure_ascii=False, separators=(",", ":"))
                                if isinstance(body_value, (dict, list))
                                else str(body_value)
                            )
                            matches = [
                                req for req in captured_requests
                                if (not method or req["method"].upper() == method)
                                and (not path or _url_matches(path, req["url"]))
                                and (not body or body in req["body"].replace(" ", ""))
                            ]
                            if not matches:
                                raise AssertionError(
                                    f"expected request method={method or '*'} path~={path!r} body~={body!r}"
                                )
                        elif action == "wait_for_response":
                            patterns = _route_patterns(
                                str(step.get("url_pattern") or step.get("url") or ""), standard)
                            timeout_ms = int(step.get("timeout_ms") or 5000)
                            def _matches_response(response):
                                for pattern in patterns:
                                    if _url_matches(pattern, response.url):
                                        return True
                                return False
                            await page.wait_for_response(_matches_response, timeout=timeout_ms)
                        elif action == "resize_viewport":
                            await page.set_viewport_size({
                                "width": int(step.get("width") or 1280),
                                "height": int(step.get("height") or 720),
                            })
                        elif action == "simulate_network_offline":
                            offline = step.get("value") if "value" in step else step.get("offline")
                            await page.context.set_offline(bool(offline))
                        elif action == "evaluate":
                            actual = await page.evaluate(str(step.get("script") or ""))
                            if "expect" in step and actual != step.get("expect"):
                                raise AssertionError(f"evaluate returned {actual!r}, expected {step.get('expect')!r}")
                        elif action == "wait_for":
                            await page.wait_for_selector(str(selector), timeout=5000)
                        else:
                            raise ValueError(f"unsupported test action: {action}")
                    logs.append(f"[{case_id}] PASS")
                except Exception as exc:
                    message = f"[{case_id}] FAIL: {type(exc).__name__}: {exc}"
                    logs.append(message)
                    failures.append(message)
        finally:
            await browser.close()
    return not failures, "\n".join(logs), failures


async def run_mcp_tests(state: TEState, cfg) -> TEState:
    from programmer1.devlog import session as devlog

    test_plan = state.get("test_plan", "")
    if not test_plan:
        reason = state.get("test_plan_error") or "无测试计划"
        return {
            "passed": False, "run_logs": reason, "summary": "❌ 无测试计划",
            "failures": [reason], "phase": "test_plan", "failure_kind": "invalid_plan",
            "requires_human": True,
            "artifacts": list(state.get("contract_artifacts", [])),
        }

    standard = state.get("standard") or cfg.human_standard
    be_ws = state.get("backend_workspace", cfg.backend_workspace)
    fe_ws = state.get("frontend_workspace", cfg.frontend_workspace)
    contract_ws = state.get("contract_workspace", getattr(cfg, "contract_workspace", ""))
    shared_contracts = state.get("shared_contracts", {})
    active_modules = state.get("active_modules", ["backend", "frontend"])
    contract_artifacts = list(state.get("contract_artifacts", []))

    if "api_contract" in getattr(standard, "all_checks", set()) and not state.get("contract_checked"):
        contract_ok, contract_log, contract_failures, repair_modules = _run_shared_contract_check(
            contract_ws, be_ws, fe_ws, shared_contracts, active_modules,
        )
        print(f"  [test_engineer/contract] {'✅' if contract_ok else '❌'}")
        devlog.log_test("api_contract", passed=contract_ok,
                        detail="; ".join(contract_failures[:2]))
        devlog.log_output("test_engineer/contract", contract_log)
        artifact = _persist_log(cfg, "contract_check.log", contract_log)
        contract_artifacts.append(artifact)
        if not contract_ok:
            return {
                "passed": False,
                "run_logs": contract_log,
                "summary": "❌ api_contract",
                "test_workspace": cfg.test_workspace,
                "test_files": [],
                "failures": contract_failures,
                "phase": "api_contract",
                "failure_kind": "contract",
                "requires_human": False,
                "artifacts": contract_artifacts,
                "repair_modules": repair_modules,
            }

    if "playwright" not in getattr(standard, "all_checks", {"playwright"}):
        return {
            "passed": True,
            "run_logs": "playwright 未在 E2E standard 中启用",
            "summary": "✅ api_contract",
            "test_workspace": cfg.test_workspace,
            "test_files": [],
            "failures": [], "phase": "api_contract", "failure_kind": "",
            "requires_human": False,
            "artifacts": contract_artifacts,
        }

    backend_payload = shared_contracts.get("backend") or {}
    if not backend_payload:
        backend_payload, _ = _backend_payload(contract_ws, shared_contracts)
        backend_payload = backend_payload or {}
    available_api_paths = [
        str(item.get("path"))
        for item in _available_api(backend_payload)
        if item.get("path")
    ]
    backend_required = _requires_backend_service(active_modules, available_api_paths)
    be_proc = fe_proc = None
    startup_artifact = ""
    try:
        be_proc, fe_proc, startup_log = await _start_services(
            be_ws, fe_ws, standard, available_api_paths, backend_required)
        startup_artifact = _persist_log(cfg, "service_startup.log", startup_log)
        devlog.log_test("service_startup", passed=True, detail=startup_log)
        devlog.log_output("test_engineer/service_startup", startup_log)
    except Exception as exc:
        reason = f"E2E 服务启动失败: {exc}"
        artifact = _persist_log(cfg, "service_startup.log", reason)
        print(f"  [test_engineer/runner] ✋ {reason}")
        devlog.log_test("service_startup", passed=False, detail=reason)
        devlog.log_output("test_engineer/service_startup", reason)
        return {
            "passed": False, "run_logs": reason, "summary": "✋ 服务启动失败",
            "test_workspace": cfg.test_workspace, "test_files": [],
            "failures": [reason], "phase": "service_startup", "failure_kind": "service",
            "requires_human": False, "artifacts": [*contract_artifacts, artifact],
            "repair_modules": active_modules,
        }

    try:
        print(f"  [test_engineer/runner] Python Playwright 执行计划 …")
        plan_obj = json.loads(test_plan)
        passed, log, failures = await asyncio.wait_for(
            _run_playwright_plan(plan_obj, standard),
            timeout=standard.test_timeout,
        )
    except asyncio.TimeoutError:
        log = f"playwright 测试超时（>{standard.test_timeout}s）"
        passed = False
        failures = [log]
    except Exception as exc:
        log = f"playwright 执行异常: {type(exc).__name__}: {exc}"
        passed = False
        failures = [log]
    finally:
        await _stop_process(be_proc)
        await _stop_process(fe_proc)

    artifact = _persist_log(cfg, "playwright_mcp.log", log)
    tag = "✅" if passed else "❌"
    print(f"  [test_engineer/runner] {tag} E2E {'通过' if passed else '失败'}")
    devlog.log_test("playwright", passed=passed,
                    detail="" if passed else "; ".join(failures[:2]))

    summary = f"{'✅' if passed else '❌'} playwright E2E"
    return {"passed": passed, "run_logs": log[-3000:], "summary": summary,
            "test_workspace": cfg.test_workspace, "test_files": [],
            "failures": [] if passed else failures,
            "phase": "playwright", "failure_kind": "" if passed else "test_failure",
            "requires_human": False,
            "artifacts": [*contract_artifacts, startup_artifact, artifact]}
