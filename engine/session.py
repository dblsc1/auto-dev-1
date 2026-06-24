"""engine/session.py — 会话缓存（session_id → 活的 SDK 句柄）。"""

from __future__ import annotations

import uuid
from typing import Any


class SessionStore:
    def __init__(self) -> None:
        self._live: dict[str, Any] = {}

    def get(self, session_id: str | None) -> Any | None:
        return self._live.get(session_id) if session_id else None

    def put(self, handle: Any, session_id: str | None = None) -> str:
        sid = session_id or uuid.uuid4().hex[:8]
        self._live[sid] = handle
        return sid

    def all_handles(self) -> list[Any]:
        return list(self._live.values())

    def clear(self) -> None:
        self._live.clear()
