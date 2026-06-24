"""schemas.py — planner-produced execution contracts.

The contract layer is intentionally broader than HTTP API contracts. It gives
the planner one structured place to state hard implementation constraints that
the worker must satisfy and the internal tester can verify mechanically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


CheckKind = Literal[
    "file",
    "function",
    "route",
    "schema",
    "dom",
    "fetch",
    "public_api",
    "consumes",
]

Severity = Literal["required", "advisory"]
RepairTarget = Literal["planner", "worker", "human", "tool"]


@dataclass
class ContractCheck:
    id: str
    kind: CheckKind
    severity: Severity = "required"
    target: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""


@dataclass
class ExecutionContract:
    module: str
    version: int = 1
    public_api: list[dict[str, Any]] = field(default_factory=list)
    internal: list[ContractCheck] = field(default_factory=list)
    consumes: list[dict[str, Any]] = field(default_factory=list)
    constraints: list[ContractCheck] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass
class ContractFailure:
    check_id: str
    kind: str
    reason: str
    repair_target: RepairTarget = "worker"


@dataclass
class ContractReport:
    passed: bool = True
    failures: list[ContractFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    logs: str = ""


def contract_check_from_dict(data: dict[str, Any]) -> ContractCheck:
    return ContractCheck(
        id=str(data.get("id") or data.get("name") or "unnamed-check"),
        kind=str(data.get("kind") or "file"),  # type: ignore[arg-type]
        severity=str(data.get("severity") or "required"),  # type: ignore[arg-type]
        target=dict(data.get("target") or {}),
        rationale=str(data.get("rationale") or ""),
    )


def execution_contract_from_dict(data: dict[str, Any], *, module: str = "") -> ExecutionContract:
    return ExecutionContract(
        module=str(data.get("module") or module),
        version=int(data.get("version") or 1),
        public_api=list(data.get("public_api") or []),
        internal=[contract_check_from_dict(x) for x in data.get("internal", [])],
        consumes=list(data.get("consumes") or []),
        constraints=[contract_check_from_dict(x) for x in data.get("constraints", [])],
        assumptions=[str(x) for x in data.get("assumptions", [])],
    )


def execution_contract_to_dict(contract: ExecutionContract) -> dict[str, Any]:
    return asdict(contract)


def _function_check_id(task_id: str, idx: int) -> str:
    return f"{task_id}-function-{idx}"


def build_contract_from_subtasks(module: str, subtasks: list[Any]) -> ExecutionContract:
    """Build a compatibility contract from existing Task fields.

    This keeps old planners working while the new planner prompt learns to emit
    execution_contract explicitly.
    """
    constraints: list[ContractCheck] = []
    internal: list[ContractCheck] = []
    public_api: list[dict[str, Any]] = []

    allowed_files = sorted({
        f
        for st in subtasks
        for f in (getattr(st, "files_allowed", []) or [])
    })
    if allowed_files:
        constraints.append(ContractCheck(
            id="files-allowed",
            kind="file",
            target={"allowed": allowed_files},
            rationale="worker may only modify planner-approved files",
        ))

    for st in subtasks:
        task_id = getattr(st, "id", "task")
        for i, spec in enumerate(getattr(st, "function_specs", []) or [], 1):
            internal.append(ContractCheck(
                id=_function_check_id(task_id, i),
                kind="function",
                target={"signature": spec},
                rationale=f"function spec from subtask {task_id}",
            ))

        contract = getattr(st, "interface_contract", {}) or {}
        path = contract.get("path")
        method = contract.get("method")
        if path and method:
            public_api.append({
                "id": task_id,
                "method": method,
                "path": path,
                "request": contract.get("request") or {},
                "response_200": contract.get("response_200") or {},
                "raw": contract,
            })
            internal.append(ContractCheck(
                id=f"{task_id}-route",
                kind="route",
                target={"method": method, "path": path},
                rationale=f"route contract from subtask {task_id}",
            ))
            request_schema = contract.get("request") or {}
            if request_schema:
                internal.append(ContractCheck(
                    id=f"{task_id}-schema",
                    kind="schema",
                    target={"fields": request_schema},
                    rationale=f"request schema from subtask {task_id}",
                ))
                internal.append(ContractCheck(
                    id=f"{task_id}-fetch",
                    kind="fetch",
                    severity="advisory",
                    target={"path": path, "fields": list(request_schema.keys())},
                    rationale="frontend workspaces should consume this HTTP contract",
                ))

    return ExecutionContract(
        module=module,
        public_api=public_api,
        internal=internal,
        constraints=constraints,
    )
