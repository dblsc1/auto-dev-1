"""Legacy node: Python process manager for generated pytest-playwright tests.

流程：
  1. 起 backend（uvicorn）
  2. 起 frontend（http.server）
  3. 等待服务就绪（轮询端口）
  4. 跑 pytest-playwright
  5. Kill 两个进程
  6. 返回 pass/fail + logs

The current graph does not mount this node. It uses ``run_mcp_tests`` instead,
which owns the same service lifecycle and drives the browser through Python
Playwright.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

from ..state import TEState


async def run_tests(state: TEState, cfg) -> TEState:
    standard  = state.get("standard") or cfg.human_standard
    ws        = state.get("test_workspace", cfg.test_workspace)
    test_files = state.get("test_files", [])

    if not test_files:
        return {"passed": False, "run_logs": "无测试文件，跳过", "summary": "❌ 无测试文件"}

    be_proc = fe_proc = None
    logs: list[str] = []

    try:
        # ① 启动 backend
        be_ws = state.get("backend_workspace", cfg.backend_workspace)
        print(f"  [test_engineer/runner] 启动 backend（port {standard.backend_port}）…")
        be_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", "0.0.0.0", "--port", str(standard.backend_port),
            cwd=be_ws,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # ② 启动 frontend
        fe_ws = state.get("frontend_workspace", cfg.frontend_workspace)
        print(f"  [test_engineer/runner] 启动 frontend（port {standard.frontend_port}）…")
        fe_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "http.server", str(standard.frontend_port),
            cwd=fe_ws,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # ③ 等待就绪
        await _wait_port(standard.backend_port,  standard.startup_timeout)
        await _wait_port(standard.frontend_port, standard.startup_timeout)
        print(f"  [test_engineer/runner] 服务就绪，跑 playwright …")

        # ④ 跑 pytest-playwright
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", "-v", "--tb=short", "--no-header",
            *test_files,
            cwd=ws,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=standard.test_timeout)
            log = stdout.decode(errors="replace")
            logs.append(log)
            passed = proc.returncode in (0, 5)
        except asyncio.TimeoutError:
            passed = False
            logs.append(f"playwright 超时（>{standard.test_timeout}s）")

    except Exception as e:
        passed = False
        logs.append(f"runner 异常: {type(e).__name__}: {e}")

    finally:
        # ⑤ Kill 进程
        for p in (be_proc, fe_proc):
            if p and p.returncode is None:
                try:
                    p.terminate()
                    await asyncio.wait_for(p.wait(), timeout=5)
                except Exception:
                    p.kill()

    combined_log = "\n".join(logs)
    tag = "✅" if passed else "❌"
    print(f"  [test_engineer/runner] {tag} playwright {'通过' if passed else '失败'}")

    # 写日志文件
    if cfg.log_path:
        Path(cfg.log_path).write_text(combined_log, encoding="utf-8")

    summary = f"{'✅' if passed else '❌'} playwright E2E"
    return {"passed": passed, "run_logs": combined_log[-3000:], "summary": summary}


async def _wait_port(port: int, timeout: int) -> None:
    """轮询直到 port 可连，超时则继续（不抛异常，让 playwright 自己报错）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.5)
