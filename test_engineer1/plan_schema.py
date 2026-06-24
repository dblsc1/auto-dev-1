"""Machine validation for AI-produced E2E test plans."""
from __future__ import annotations

from typing import Any


SUPPORTED_ACTIONS = {
    "navigate",
    "fill",
    "click",
    "press",
    "wait",
    "wait_for_timeout",
    "expect_visible",
    "expect_present",
    "expect_exists",
    "expect_attached",
    "expect_not_visible",
    "expect_text",
    "expect_attribute",
    "expect_not_attribute",
    "expect_class",
    "expect_computed_style",
    "expect_count",
    "expect_request",
    "expect_no_request",
    "expect_no_request_to",
    "assert_no_request_to",
    "expect_no_request_to_path",
    "route_intercept",
    "route",
    "route_clear",
    "resize_viewport",
    "simulate_network_offline",
    "evaluate",
    "wait_for",
    "wait_for_response",
    "start_request_capture",
    "setup_network_capture",
}


def _has_any(step: dict[str, Any], *keys: str) -> bool:
    return any(key in step and step[key] not in ("", None) for key in keys)


def _text_expectation(step: dict[str, Any]) -> str:
    return str(step.get("contains") or step.get("equals") or step.get("exact") or "")


def validate_test_plan(plan: dict[str, Any]) -> list[str]:
    """Return machine-actionable schema errors for a Playwright plan."""
    errors: list[str] = []
    cases = plan.get("test_cases")
    if not isinstance(cases, list) or not cases:
        return ["test_cases must be a non-empty list"]
    if len(cases) > 8:
        errors.append("test_cases should contain at most 8 cases")

    for case_index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            errors.append(f"test_cases[{case_index}] must be an object")
            continue
        case_id = str(case.get("id") or f"case-{case_index}")
        steps = case.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{case_id}: steps must be a non-empty list")
            continue

        has_user_trigger = False
        has_delayed_route = False
        for step_index, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                errors.append(f"{case_id}.steps[{step_index}] must be an object")
                continue
            action = str(step.get("action") or "")
            prefix = f"{case_id}.steps[{step_index}] {action or '<missing action>'}"
            if action not in SUPPORTED_ACTIONS:
                errors.append(f"{prefix}: unsupported action")
                continue

            if action == "navigate" and not _has_any(step, "url"):
                errors.append(f"{prefix}: url is required")
            elif action in {"fill", "click", "expect_visible", "expect_present",
                            "expect_exists", "expect_attached", "expect_not_visible",
                            "expect_text", "expect_class", "expect_attribute",
                            "expect_not_attribute", "expect_count", "wait_for"}:
                if not _has_any(step, "selector"):
                    errors.append(f"{prefix}: selector is required")
            elif action == "press" and not _has_any(step, "key"):
                errors.append(f"{prefix}: key is required")
            elif action in {"route_intercept", "route"}:
                if not _has_any(step, "url_pattern", "url", "path"):
                    errors.append(f"{prefix}: url_pattern, url, or path is required")
                if not _has_any(step, "strategy", "route_action", "mode", "status",
                                "body", "delay_ms", "delay"):
                    errors.append(
                        f"{prefix}: route_intercept must declare strategy/status/body/delay"
                    )
                if _has_any(step, "delay_ms", "delay"):
                    has_delayed_route = True
            elif action == "expect_request":
                if not _has_any(step, "path", "path_contains", "url_pattern",
                                "url_contains", "url"):
                    errors.append(f"{prefix}: request path/url is required")
                if not has_user_trigger:
                    errors.append(f"{prefix}: expect_request must follow the triggering user action")
            elif action in {"expect_no_request", "expect_no_request_to", "assert_no_request_to",
                            "expect_no_request_to_path"}:
                if not _has_any(step, "path", "path_contains", "url_pattern",
                                "url_contains", "url"):
                    errors.append(f"{prefix}: request path/url is required")
            elif action == "simulate_network_offline":
                if "value" not in step and "offline" not in step:
                    errors.append(f"{prefix}: value/offline boolean is required")

            if action in {"click", "press"}:
                has_user_trigger = True

            if action == "expect_text":
                text = _text_expectation(step)
                if text in {"Loading", "Loading...", "登录中", "登录中..."} and not has_delayed_route:
                    errors.append(f"{prefix}: loading assertions require a prior delayed route_intercept")

    return errors
