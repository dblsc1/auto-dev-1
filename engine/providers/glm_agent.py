"""engine/providers/glm_agent.py — CC SDK + ZhipuAI GLM API。

复用 claude-agent-sdk agent loop（tool-use、session），
通过 env 把 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 指向 GLM 端点。
GLM 需要提供 Anthropic 兼容接口（或通过代理适配）。
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

_AUTH_FAIL_MARKERS = (
    "authentication fails",
    "authentication failed",
    "unauthorized",
    "invalid api key",
    "api error:",
)


def _looks_like_auth_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(tag in lowered for tag in _AUTH_FAIL_MARKERS)


async def _deny_dangerous(tool_name: str, input_data: dict, context) -> object:
    if tool_name == "Bash":
        cmd = (input_data or {}).get("command", "")
        for bad in _DANGEROUS:
            if bad in cmd:
                return PermissionResultDeny(
                    message=f"权限闸拒绝危险命令（含 '{bad}'）：{cmd[:120]}")
    return PermissionResultAllow()


class _Session:
    def __init__(self, client: ClaudeSDKClient):
        self.client = client
        self.lock = asyncio.Lock()


class GLMAgentProvider(Provider):
    """CC SDK agent loop，后端换成 GLM API（需要 GLM_API_KEY + Anthropic 兼容端点）。"""
    name = "glm"

    def __init__(self, store: SessionStore, api_key: str, base_url: str):
        self.store = store
        self._env = {
            "ANTHROPIC_BASE_URL":             base_url,
            "ANTHROPIC_AUTH_TOKEN":           api_key,
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
        }
        self._has_key = bool(api_key)

    async def run(self, *, prompt, model, cwd=".", env=None, session_id=None,
                  allowed_tools=None, system_prompt=None, effort=None,
                  readonly_dirs=None, mcp_servers=None) -> AgentResult:
        if not self._has_key:
            return AgentResult(text="", ok=False,
                               error="GLMAgentProvider: GLM_API_KEY 未设置")
        try:
            sess: _Session | None = self.store.get(session_id)
            if sess is None:
                options = ClaudeAgentOptions(
                    model=model, cwd=cwd,
                    add_dirs=readonly_dirs or [],
                    env=self._env,
                    allowed_tools=allowed_tools if allowed_tools is not None else [],
                    permission_mode="acceptEdits",
                    can_use_tool=_deny_dangerous,
                    effort=effort,
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
            if _looks_like_auth_failure(text):
                return AgentResult(text="", session_id=None, ok=False,
                                   error=f"GLM 401 认证失败（key 无效）: {text[:200]}")
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
