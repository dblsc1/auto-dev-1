"""engine/registry.py — smart_level → EngineSpec 热插拔表。

改这里 = 换引擎/模型/effort，上层代码零修改。
凭证统一在 engine_config.py 读，不在这里。

【六条通道】
  level 6  codex        effort=high              Codex 高算力（替代 Opus）
  level 5  codex        effort=high              Codex 规划/裁决
  level 4  codex        effort=high              Codex 语义审查（替代 Sonnet）
  level 3  deepseek     deepseek-v4-pro          Claude Code SDK + DeepSeek Pro，需 key
  level 2  glm          glm-4                    Claude Code SDK + GLM，需 key；无 key/API 错误 → 升 L3
  level 1  deepseek     deepseek-v4-flash        Claude Code SDK + DeepSeek Flash，需 key；无 key/API 错误 → 升 L3

改 Codex 版本：把 level 5 的 model 改成 "codex-5.4" 等即可。
改 effort  ：按 level 调整 effort 即可。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EngineSpec:
    provider: str          # 对应 agent.py _PROVIDERS 的 key
    model:    str          # 传给 provider.run()；空串 = provider 自己决定
    effort:   str | None = None   # Codex 专用：high / medium / low


REGISTRY: dict[int, EngineSpec] = {
    6: EngineSpec("codex", "", effort="high"),
    5: EngineSpec("codex", "", effort="high"),
    4: EngineSpec("codex", "", effort="high"),
    3: EngineSpec("deepseek", "deepseek-v4-pro"),
    2: EngineSpec("glm",      "glm-4"),
    1: EngineSpec("deepseek", "deepseek-v4-flash"),
}

DEFAULT_LEVEL = 5          # 未知 smart_level 的兜底：Codex 规划/裁决通道
LOW_LEVEL_FALLBACK = 3     # level 1/2 provider/key/API 错误时仍留在 L1-L3 模型通道


def resolve(smart_level: int) -> EngineSpec:
    return REGISTRY.get(smart_level, REGISTRY[DEFAULT_LEVEL])
