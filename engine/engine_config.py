"""engine/engine_config.py — Provider 凭证统一收口。

所有 API key / 路径在这里一次性从 env 读取，providers 只接受 ProviderConfig 对象，
不自己读 os.environ。必须在 os.environ.pop("ANTHROPIC_API_KEY") 之前实例化。

【五条通道】
  cc          — CC Pro 订阅，本地 credentials.json，无需额外 key
  codex       — Codex CLI 订阅，本地进程，CODEX_PATH 可覆盖
  deepseek    — CC SDK + DeepSeek API，需 DEEPSEEK_API_KEY
  glm         — CC SDK + GLM API，需 GLM_API_KEY + Anthropic 兼容端点
  （模型/effort 在 registry.py 配置，不在这里）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class ProviderConfig:
    # ── Codex CLI（CC 订阅，本地进程）──
    codex_bin: str = field(
        default_factory=lambda: os.environ.get(
            "CODEX_PATH", os.path.expanduser("~/.hermes/node/bin/codex")))

    # ── CC SDK + DeepSeek ──
    deepseek_api_key: str = field(
        default_factory=lambda: os.environ.get("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = "https://api.deepseek.com/anthropic"

    # ── CC SDK + GLM（需要 Anthropic 兼容接口或代理）──
    glm_api_key: str = field(
        default_factory=lambda: os.environ.get("GLM_API_KEY", ""))
    glm_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/anthropic"))
