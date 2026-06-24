"""engine/providers/cc_agent.py — Claude Code Pro 订阅 Agent。

使用 claude-agent-sdk，env={} 继承父进程登录态（~/.claude/.credentials.json）。
适合所有需要 tool-use 的编码任务（planner / worker / reviewer）。
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk import (
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, TextBlock, ResultMessage,
    PermissionResultAllow, PermissionResultDeny,
)

from .base import Provider, AgentResult
from ..session import SessionStore

_DANGEROUS = ("rm -rf", "rm -fr", "rm  -rf", "sudo ", "mkfs", ":(){",
              "dd if=", "> /dev/sd", "chmod -R 777 /", "git push")


def _make_guard(workspace: str):
    """返回 can_use_tool 回调，同时拦截危险命令 + workspace 外写操作。"""
    import os
    ws = os.path.realpath(os.path.abspath(workspace))

    def _in_workspace(path: str) -> bool:
        try:
            return os.path.realpath(os.path.abspath(path)).startswith(ws)
        except Exception:
            return False

    async def _guard(tool_name: str, input_data: dict, context) -> object:
        data = input_data or {}

        # ── 危险命令黑名单 ────────────────────────────────────────────────────
        if tool_name == "Bash":
            cmd = data.get("command", "")
            for bad in _DANGEROUS:
                if bad in cmd:
                    return PermissionResultDeny(
                        f"权限闸拒绝危险命令（含 '{bad}'）：{cmd[:120]}")

        # ── 路径写越界检查 ────────────────────────────────────────────────────
        if tool_name in ("Write", "Edit", "NotebookEdit"):
            path = data.get("file_path", "")
            if path and not _in_workspace(path):
                return PermissionResultDeny(
                    f"路径越界：{path!r} 不在 workspace {ws!r} 内，拒绝写入")

        # Bash 里的重定向写操作：扫 '> /path' 或 'tee /path' 到 workspace 外
        if tool_name == "Bash":
            cmd = data.get("command", "")
            import re, shlex
            for m in re.finditer(r'(?:>>?|tee)\s+([^\s;|&]+)', cmd):
                path = m.group(1).strip("'\"")
                if path.startswith("/") and not _in_workspace(path):
                    return PermissionResultDeny(
                        f"路径越界：Bash 重定向写 {path!r} 不在 workspace 内，拒绝")

        return PermissionResultAllow()

    return _guard


class _Session:
    def __init__(self, client: ClaudeSDKClient):
        self.client = client
        self.lock = asyncio.Lock()


class CCAgentProvider(Provider):
    """Claude Code Pro 订阅 — 不烧 token，走本地 credentials.json。"""
    name = "cc"

    def __init__(self, store: SessionStore):
        self.store = store

    async def run(self, *, prompt, model, cwd=".", env=None, session_id=None,
                  allowed_tools=None, system_prompt=None, effort=None,
                  readonly_dirs=None, mcp_servers=None) -> AgentResult:
        try:
            sess: _Session | None = self.store.get(session_id)
            if sess is None:
                options = ClaudeAgentOptions(
                    model=model, cwd=cwd,
                    add_dirs=readonly_dirs or [],
                    env={},                          # 继承父进程登录态
                    allowed_tools=allowed_tools if allowed_tools is not None else [],
                    permission_mode="acceptEdits",
                    can_use_tool=_make_guard(cwd),   # 路径守卫绑定到此次 workspace
                    effort=effort,
                    mcp_servers=mcp_servers or {},
                    system_prompt=(
                        {"type": "preset", "preset": "claude_code", "append": system_prompt}
                        if system_prompt else None
                    ),
                )
                client = ClaudeSDKClient(options=options)
                await client.connect()
                sess = _Session(client)
                session_id = self.store.put(sess, session_id)

            async with sess.lock:
                await sess.client.query(prompt)
                text = ""
                async for m in sess.client.receive_response():
                    if isinstance(m, AssistantMessage):
                        for b in m.content:
                            if isinstance(b, TextBlock):
                                text += b.text
                                print(b.text, end="", flush=True)
                    elif isinstance(m, ResultMessage):
                        if m.result:
                            text = m.result
                if text and not text.endswith("\n"):
                    print()
            return AgentResult(text=text, session_id=session_id, ok=True)
        except Exception as e:
            return AgentResult(text="", session_id=session_id, ok=False,
                               error=f"{type(e).__name__}: {e}")

    async def close(self) -> None:
        for sess in self.store.all_handles():
            try:
                await sess.client.disconnect()
            except Exception:
                pass
        self.store.clear()
