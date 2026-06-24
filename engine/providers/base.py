"""engine/providers/base.py — Provider 抽象 + 统一返回结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentResult:
    text: str
    session_id: str | None = None
    changed_files: list[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    # Provider-independent classification used by the workflow to decide
    # whether retrying code is meaningful. A quota error must stop for a human.
    failure_kind: str = ""


class Provider(ABC):
    name: str

    @abstractmethod
    async def run(
        self,
        *,
        prompt: str,
        model: str,
        cwd: str = ".",
        env: dict[str, str] | None = None,
        session_id: str | None = None,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        effort: str | None = None,
        readonly_dirs: list[str] | None = None,
    ) -> AgentResult: ...

    async def close(self) -> None: ...
