A：每种模型独立文件 ✅

  engine/providers/
    base.py            —— Provider ABC + AgentResult（不变）
    cc_agent.py        —— CC Pro 订阅 agent（原 claude.py CC 部分）
    deepseek_agent.py  —— CC SDK + DeepSeek API（原 claude.py DeepSeek 部分）
    anthropic_chat.py  —— Anthropic Messages API，httpx（arbiter 用）
    glm_chat.py        —— ZhipuAI GLM，OpenAI 兼容 httpx（新增）
    codex.py           —— Codex CLI 订阅（codex_bin 改为构造参数）
    claude.py          —— 已删除

  B：凭证统一收口 ✅

  engine/engine_config.py   ← 所有 key / 路径在此一次性读取
  engine/agent.py           ← 顺序：ProviderConfig() → pop env → 初始化 Provider
  engine/registry.py        ← 只有 provider 名 + model，无凭证

  新加模型只需 3 步

  1. providers/xxx.py — 继承 Provider，实现 run() 返回 AgentResult
  2. engine_config.py — 加 xxx_api_key 字段
  3. registry.py — 加一行 N: EngineSpec("xxx", "model-name")，agent.py 里注册

  如何测试引擎层

  cd growth-garden-builder && source .venv/bin/activate
  # 验证 5 个 provider 全部注册
  PYTHONPATH=. python -c "
  from dotenv import load_dotenv; load_dotenv()
  from programmer1.engine.agent import _PROVIDERS
  print(list(_PROVIDERS.keys()))  # ['cc','codex','deepseek','anthropic_chat','glm']
  "

  # 测 anthropic_chat（需要 ANTHROPIC_API_KEY）
  PYTHONPATH=. python -c "
  import asyncio
  from dotenv import load_dotenv; load_dotenv()
  from programmer1.engine.agent import run_agent
  asyncio.run(run_agent(prompt='hello', smart_level=6, system_prompt='简洁回答'))
  " && echo ok
