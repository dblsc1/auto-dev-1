"""engine/agent.py — 对外唯一入口：run_agent / run_parallel / Job。

上层永远不碰 SDK / httpx，只调这两个函数。
smart_level → registry → provider，热插拔。

初始化顺序（必须严格保持）：
  1. ProviderConfig()  读 env（含任意 API key）
  2. os.environ.pop()  清掉 key，强制 CC agent 走本地登录态
  3. 初始化各 Provider（key 已在 cfg 里安全保存）

【Fallback 规则】
  level 1/2/3 保留 Claude Code SDK + DeepSeek/GLM 工具链；
  level 1/2 无 key 或 API 错误时只升到 level 3 DeepSeek Pro；
  level 3 再失败则上报模块失败，不再偷跑 Codex。
"""

from __future__ import annotations

import os
import asyncio
import time
from dataclasses import dataclass, field

from .engine_config import ProviderConfig

# ① 读凭证（必须在 pop 之前）
_cfg = ProviderConfig()

# ② 清掉 Anthropic key，强制 CC agent 走本地订阅
os.environ.pop("ANTHROPIC_API_KEY",    None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# ③ 导入 Provider（此时 env 已干净）
from .registry import resolve, LOW_LEVEL_FALLBACK
from .session  import SessionStore
from .providers.base           import AgentResult
from .providers.cc_agent       import CCAgentProvider
from .providers.codex          import CodexProvider
from .providers.deepseek_agent import DeepSeekAgentProvider
from .providers.glm_agent      import GLMAgentProvider

_STORE = SessionStore()

_PROVIDERS = {
    "cc":       CCAgentProvider(_STORE),
    "codex":    CodexProvider(_cfg.codex_bin),
    "deepseek": DeepSeekAgentProvider(_STORE, _cfg.deepseek_api_key, _cfg.deepseek_base_url),
    "glm":      GLMAgentProvider(_STORE, _cfg.glm_api_key, _cfg.glm_base_url),
}

# 无 key 时要升级的 provider 名称
_NEEDS_KEY = {"deepseek", "glm"}

_USAGE_LIMIT_MARKERS = (
    "usage limit",
    "api limit reached",
    "rate limit",
    "quota",
    "insufficient balance",
    "session limit",
    "hit your session limit",
    "limit reached",
    "额度",
)

_PROVIDER_ERROR_MARKERS = (
    "api error:",
    "overloaded",
    "authentication fails",
    "authentication failed",
    "invalid api key",
)


def is_usage_limit_result(result: AgentResult) -> bool:
    """Return true for provider quota/session failures, including text-only SDK errors."""
    if result.failure_kind == "usage_limit":
        return True
    text = " ".join((result.error or "", result.text or "")).lower()
    return any(marker in text for marker in _USAGE_LIMIT_MARKERS)


def _usage_limit_detail(result: AgentResult) -> str:
    raw = (result.error or result.text or "provider reported a usage/session limit").strip()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    relevant = [
        line for line in lines
        if any(marker in line.lower() for marker in _USAGE_LIMIT_MARKERS)
        or line.lower().startswith("error:")
    ]
    if relevant:
        return "\n".join(relevant[:3])[:500]
    return raw[:500]


def _normalize_result(result: AgentResult) -> AgentResult:
    if is_usage_limit_result(result):
        detail = _usage_limit_detail(result)
        message = f"provider usage/session limit: {detail[:500]}"
        return AgentResult(
            text=result.text,
            session_id=result.session_id,
            changed_files=result.changed_files,
            ok=False,
            error=message,
            failure_kind="usage_limit",
        )
    text = " ".join((result.error or "", result.text or "")).lower()
    if result.ok and any(marker in text for marker in _PROVIDER_ERROR_MARKERS):
        detail = (result.error or result.text or "provider reported an API error").strip()
        return AgentResult(
            text=result.text,
            session_id=result.session_id,
            changed_files=result.changed_files,
            ok=False,
            error=detail[:500],
            failure_kind="provider",
        )
    return result

# ── 启动时打印 key 缺失警告，方便监督者提前知晓成本影响 ──────────────────────
if not _cfg.glm_api_key:
    print("[engine] ⚠ GLM_API_KEY 未设置 — L2 任务将自动升级到 L3 (deepseek)")
if not _cfg.deepseek_api_key:
    print("[engine] ⚠ DEEPSEEK_API_KEY 未设置 — L1/L3 任务将失败并上报，不自动使用 Codex")


_PROVIDER_FAILURE_FALLBACK_MARKERS = (
    "未设置",
    "401",
    "400",
    "认证失败",
    "api 错误",
    "api error",
    "overloaded",
    "authentication fails",
    "authentication failed",
    "invalid api key",
)


def fallback_level_for_provider_failure(smart_level: int, provider: str, error: str | None) -> int | None:
    """Keep L1-L3 worker failures inside the low-level model lane.

    Codex is reserved for configured L4-L6 roles. If L3 itself cannot run, the
    caller must see the failure so arbiter can stop or retry explicitly.
    """
    if provider not in _NEEDS_KEY or smart_level >= LOW_LEVEL_FALLBACK:
        return None
    lowered = (error or "").lower()
    if not any(marker in lowered for marker in _PROVIDER_FAILURE_FALLBACK_MARKERS):
        return None
    return LOW_LEVEL_FALLBACK


@dataclass
class Job:
    prompt:        str
    smart_level:   int               = 4
    session_id:    str | None        = None
    cwd:           str               = "."
    allowed_tools: list[str] | None  = None
    system_prompt: str | None        = None
    readonly_dirs: list[str] | None  = None


async def run_agent(
    *,
    prompt:        str,
    smart_level:   int               = 4,
    session_id:    str | None        = None,
    cwd:           str               = ".",
    allowed_tools: list[str] | None  = None,
    system_prompt: str | None        = None,
    readonly_dirs: list[str] | None  = None,
    mcp_servers:   dict | None       = None,
    trace_name:    str               = "agent",
) -> AgentResult:
    spec     = resolve(smart_level)
    provider = _PROVIDERS.get(spec.provider)
    started = time.monotonic()

    if provider is None:
        result = AgentResult(text="", ok=False,
                             error=f"未知 provider: {spec.provider}（registry 配置有误）",
                             failure_kind="provider")
        _log_agent_result(trace_name, spec.provider, spec.model, smart_level, result, started)
        return result

    result = _normalize_result(await provider.run(
        prompt=prompt, model=spec.model, cwd=cwd, env=None,
        session_id=session_id, allowed_tools=allowed_tools,
        system_prompt=system_prompt, effort=spec.effort,
        readonly_dirs=readonly_dirs, mcp_servers=mcp_servers,
    ))

    target = fallback_level_for_provider_failure(smart_level, spec.provider, result.error)
    if not result.ok and not is_usage_limit_result(result) and target is not None:
        fallback_spec = resolve(target)
        print(f"  [engine] level={smart_level}({spec.provider}) 无 key / API 错误"
              f" → 升级 level={target}({fallback_spec.provider})")
        _log_agent_result(trace_name, spec.provider, spec.model, smart_level, result, started)
        return await run_agent(
            prompt=prompt, smart_level=target,
            session_id=session_id, cwd=cwd,
            allowed_tools=allowed_tools, system_prompt=system_prompt,
            readonly_dirs=readonly_dirs,
            mcp_servers=mcp_servers,
            trace_name=trace_name,
        )

    _log_agent_result(trace_name, spec.provider, spec.model, smart_level, result, started)
    return result


def _log_agent_result(
    trace_name: str,
    provider: str,
    model: str,
    level: int,
    result: AgentResult,
    started: float,
) -> None:
    """Keep complete per-call output in detailed devlogs without coupling callers to logging."""
    from ..devlog import session as devlog

    devlog.log_model_call(
        trace_name=trace_name,
        provider=provider,
        model=model or "default",
        level=level,
        elapsed_seconds=time.monotonic() - started,
        ok=result.ok,
        failure_kind=result.failure_kind,
        output=result.text or result.error,
    )


async def run_parallel(jobs: list[Job]) -> list[AgentResult]:
    return await asyncio.gather(*(
        run_agent(prompt=j.prompt, smart_level=j.smart_level, session_id=j.session_id,
                  cwd=j.cwd, allowed_tools=j.allowed_tools, system_prompt=j.system_prompt,
                  readonly_dirs=j.readonly_dirs)
        for j in jobs
    ))


async def shutdown() -> None:
    for p in _PROVIDERS.values():
        await p.close()
