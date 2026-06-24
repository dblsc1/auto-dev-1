"""lite_tester/runners/playwright_runner.py — Playwright 前端 E2E 测试。

Godot 暂无 Web 测试套件，run_playwright=False 时这个文件不会被调用。
等到 Godot 项目有 Playwright 测试配置后，才真正激活（TesterConfig.run_playwright=True）。
"""

from __future__ import annotations

import asyncio
import os


async def run_playwright(workspace: str, timeout: int = 120) -> tuple[bool, str]:
    """返回 (passed, log_text)。需要 workspace 里有 playwright.config.* 文件。"""
    config_exists = any(
        os.path.exists(os.path.join(workspace, f))
        for f in ("playwright.config.js", "playwright.config.ts", "playwright.config.mjs")
    )
    if not config_exists:
        return False, f"未找到 playwright.config.*（workspace={workspace}）"
    try:
        proc = await asyncio.create_subprocess_exec(
            "npx", "playwright", "test", "--reporter=line",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        log = stdout.decode(errors="replace")
        return proc.returncode == 0, log
    except asyncio.TimeoutError:
        return False, f"playwright 超时（>{timeout}s）"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
