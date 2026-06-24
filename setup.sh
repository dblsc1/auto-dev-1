#!/usr/bin/env bash
set -e

echo "=== programmer1 环境检查 ==="

# 1) codex
CODEX_BIN="${CODEX_PATH:-$HOME/.hermes/node/bin/codex}"
if [ -x "$CODEX_BIN" ]; then
    echo "✅ codex 找到：$CODEX_BIN"
else
    echo "⚠  codex 未找到（路径 $CODEX_BIN）"
    echo "   → npm install -g @openai/codex 后登录：codex auth login"
fi

# 2) claude code (CC)
if command -v claude &>/dev/null; then
    echo "✅ claude 找到：$(command -v claude)"
else
    echo "⚠  claude 未找到"
    echo "   → 安装 Claude Code CLI 后运行 claude 登录一次"
    echo "   → 凭证会落在 ~/.claude/.credentials.json，程序自动读取"
fi

# 3) .env
if [ -f .env ]; then
    echo "✅ .env 存在"
else
    cp .env.example .env
    echo "✅ 已从 .env.example 生成 .env，请填入 DEEPSEEK_API_KEY"
fi

echo ""
echo "=== 依赖安装 ==="
python -m venv .venv
.venv/bin/pip install -r requirements.txt -q
echo "✅ 依赖安装完成"

echo ""
echo "=== 完成！跑法 ==="
echo "  .venv/bin/python -m programmer1.graph          # stub 全景（免费）"
echo "  .venv/bin/python -m programmer1.run '做登录'   # 真跑整体"
echo "  .venv/bin/python -m programmer1.modules.backend  # 单跑后端"
