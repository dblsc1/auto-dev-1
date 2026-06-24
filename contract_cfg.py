"""contract_cfg.py — shared API contract workspace configuration."""

from __future__ import annotations

from pathlib import Path

CONTRACT_WORKSPACE = str(Path.home() / "gg-workspace" / "contracts")
BACKEND_CONTRACT_FILE = "backend_api.json"
CONSUMER_CONTRACT_DIR = "consumers"


def ensure_contract_workspace() -> str:
    path = Path(CONTRACT_WORKSPACE)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
