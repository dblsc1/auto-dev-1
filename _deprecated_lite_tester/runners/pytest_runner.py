"""lite_tester/runners/pytest_runner.py — 在 backend_workspace 跑 pytest。"""

from __future__ import annotations

import asyncio
import sys


async def run_pytest(workspace: str, timeout: int = 60) -> tuple[bool, str]:
    """返回 (passed, log_text)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        log = stdout.decode(errors="replace")
        # exit code 5 = no tests collected，视为通过（没有测试≠失败）
        passed = proc.returncode in (0, 5)
        return passed, log
    except asyncio.TimeoutError:
        return False, f"pytest 超时（>{timeout}s）"
    except FileNotFoundError:
        return False, f"workspace 不存在或 pytest 未安装：{workspace}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
