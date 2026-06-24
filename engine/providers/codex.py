"""engine/providers/codex.py — codex CLI 包成统一 Provider（Codex 订阅，不烧 token）。"""

from __future__ import annotations

import os
import asyncio
import tempfile

from .base import Provider, AgentResult


class CodexProvider(Provider):
    """Codex CLI 订阅（subprocess），凭证路径由 ProviderConfig 传入。"""
    name = "codex"

    def __init__(self, codex_bin: str):
        self._bin = codex_bin

    async def run(self, *, prompt, model, cwd=".", env=None, session_id=None,
                  allowed_tools=None, system_prompt=None, effort=None,
                  readonly_dirs=None, mcp_servers=None) -> AgentResult:
        out_file = tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False)
        out_file.close()
        cmd = [self._bin, "exec", "-C", cwd,
               "--sandbox", "workspace-write",
               "-c", 'approval_policy="never"',
               "--skip-git-repo-check",
               "-o", out_file.name]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        for d in (readonly_dirs or []):
            cmd += ["--add-dir", d]

        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        sub_env = {**os.environ, **(env or {})}

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=sub_env,
            )
            proc.stdin.write(full_prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

            # 实时打印 codex stdout（agent 工作过程）
            async for line in proc.stdout:
                print(line.decode(errors="replace"), end="", flush=True)

            await proc.wait()
            stderr_bytes = await proc.stderr.read()

            try:
                with open(out_file.name) as f:
                    text = f.read().strip()
            except Exception:
                text = ""
            ok = proc.returncode == 0
            err = "" if ok else (stderr_bytes.decode()[-500:] if stderr_bytes else f"exit {proc.returncode}")
            return AgentResult(text=text, session_id=session_id, ok=ok, error=err)
        except Exception as e:
            return AgentResult(text="", session_id=session_id, ok=False,
                               error=f"{type(e).__name__}: {e}")
        finally:
            try:
                os.unlink(out_file.name)
            except Exception:
                pass

    async def close(self) -> None:
        pass
