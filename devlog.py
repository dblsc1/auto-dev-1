"""devlog.py — 会话级开发日志，写到 ~/gg-workspace/devlog.md，保留最近 3 次。

用法：
    from programmer1.devlog import session as devlog
    devlog.start("做个2048游戏", detailed=True)
    devlog.log_batch(0, tasks, reports)
    devlog.log_test("pytest", passed=True, detail="3 passed")
    devlog.finish("全部完成")
    devlog.save()    # 落盘，自动裁剪到 3 次
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .schemas import Task, ModuleReport

_DEVLOG_PATH = Path.home() / "gg-workspace" / "devlog.md"
_MAX_SESSIONS = 3
_SESSION_SEP  = "\n\n---\n\n"


class DevlogSession:
    def __init__(self) -> None:
        self._ts:       str       = ""
        self._req:      str       = ""
        self._lines:    list[str] = []
        self._active:   bool      = False
        self._detailed: bool      = False

    def _now(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _append(self, text: str = "") -> None:
        self._lines.append(text)

    def _entry(self, msg: str) -> None:
        self._append(f"- `{self._now()}` {msg}")

    def start(self, requirement: str, *, detailed: bool = False) -> None:
        self._ts       = self._now()
        self._req      = requirement
        self._detailed = detailed
        mode = "detailed" if detailed else "simple"
        self._lines    = [
            f"# Session {self._ts}",
            "",
            f"**Mode:** `{mode}`",
            f"**Requirement:** {requirement}",
            "",
            "## Timeline",
        ]
        self._active = True
        self._entry("session started")

    @property
    def detailed(self) -> bool:
        return self._detailed

    def log_batch(self, batch_idx: int, tasks: list, reports: list) -> None:
        if not self._active:
            return
        self._append("")
        self._append(f"## Batch {batch_idx}")
        self._entry(f"batch {batch_idx} completed")
        for t in tasks:
            self._append(f"- [{t.owner}] `{t.id}` {t.title}")
        if reports:
            self._append("")
            self._append("Results:")
            for r in reports:
                if hasattr(r, "module"):
                    icon = "✅" if r.ok else "❌"
                    summary = getattr(r, "summary", "")[:120]
                    note = getattr(r, "note", "")
                    self._append(f"  {icon} {r.module}: {summary}")
                    if self._detailed and note:
                        self._append(f"    note: {note}")
        self._append("")

    def log_test(self, check_name: str, *, passed: bool, detail: str = "") -> None:
        if not self._active:
            return
        icon = "✅" if passed else "❌"
        tail = f" — {detail}" if detail else ""
        self._entry(f"test `{check_name}`: {icon}{tail}")

    def log_event(self, msg: str, *, detailed: bool = False) -> None:
        if not self._active:
            return
        if detailed and not self._detailed:
            return
        self._entry(msg)

    def log_output(
        self,
        source: str,
        text: str,
        *,
        detailed: bool = True,
        max_chars: int | None = None,
    ) -> None:
        """Record raw-ish output. In simple mode detailed output is skipped."""
        if not self._active:
            return
        if detailed and not self._detailed:
            return
        safe = (text or "").strip()
        if not safe:
            return
        truncated = max_chars is not None and len(safe) > max_chars
        if truncated:
            safe = safe[-max_chars:]
        self._append("")
        self._append(f"### `{self._now()}` {source}")
        if truncated:
            self._append(f"_truncated to last {max_chars} chars_")
        self._append("```text")
        self._append(safe)
        self._append("```")

    def log_model_call(
        self,
        *,
        trace_name: str,
        provider: str,
        model: str,
        level: int,
        elapsed_seconds: float,
        ok: bool,
        failure_kind: str,
        output: str,
    ) -> None:
        """Record every provider result in detailed mode and a compact receipt otherwise."""
        if not self._active:
            return
        status = "✅" if ok else "❌"
        suffix = f" failure_kind={failure_kind}" if failure_kind else ""
        receipt = (
            f"model `{trace_name}` {status} {provider}/{model} L{level} "
            f"{elapsed_seconds:.1f}s{suffix}"
        )
        self._entry(receipt)
        if self._detailed:
            self.log_output(
                f"model output: {trace_name} ({provider}/{model}, L{level}, {elapsed_seconds:.1f}s)",
                output,
                detailed=True,
            )

    def finish(self, outcome: str) -> None:
        if not self._active:
            return
        self._append("")
        self._entry(f"outcome: {outcome}")
        self._append(f"\n**Outcome:** {outcome}")

    def save(self) -> None:
        if not self._active:
            return
        _DEVLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        new_block = "\n".join(self._lines).strip()

        existing = _DEVLOG_PATH.read_text(encoding="utf-8") if _DEVLOG_PATH.exists() else ""
        old_sessions = [s.strip() for s in existing.split(_SESSION_SEP.strip()) if s.strip()]

        # keep last (MAX_SESSIONS - 1) old sessions + new one
        kept = old_sessions[-(  _MAX_SESSIONS - 1):] if old_sessions else []
        kept.append(new_block)

        _DEVLOG_PATH.write_text(_SESSION_SEP.join(kept) + "\n", encoding="utf-8")
        print(f"[devlog] 已写入 {_DEVLOG_PATH}（保留最近 {_MAX_SESSIONS} 次）")
        self._active = False


# module-level singleton — import and use directly
session = DevlogSession()
