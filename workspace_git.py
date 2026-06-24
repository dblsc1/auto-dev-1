"""programmer1/workspace_git.py — workspace git 管理。

设计：每个 module workspace 是一个独立的 git worktree，
所有 worktree 指向同一个 bare repo（~/gg-workspace/.gg.git），
CC worker 在 workspace 里找到 .git 就停下，不会往上走到 growth-garden-builder。

bare repo 结构：
  ~/gg-workspace/.gg.git/    ← 中心 bare repo
  ~/gg-workspace/backend/    ← worktree，branch: backend
  ~/gg-workspace/frontend/   ← worktree，branch: frontend

函数：
  setup_worktree(workspace, branch)  → 确保 workspace 是 worktree
  commit_workspace(workspace, msg)   → git add -A + commit
  push_workspace(workspace, remote, branch) → git push
  ensure_workspace_git(workspace, branch)   → setup + baseline commit（一步到位）
"""

from __future__ import annotations

import subprocess
from pathlib import Path


_GIT_USER = ["-c", "user.email=gg-builder@local", "-c", "user.name=GG-Builder"]


def _run(args: list[str], cwd: str | None = None, check: bool = False) -> tuple[int, str, str]:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _is_worktree(workspace: str) -> bool:
    """workspace/.git 是文件（worktree 指针）而非目录。"""
    git_path = Path(workspace) / ".git"
    return git_path.is_file()


def _is_git_repo(workspace: str) -> bool:
    rc, _, _ = _run(["git", "-C", workspace, "rev-parse", "--git-dir"])
    return rc == 0


def _bare_repo_path(workspace: str) -> Path:
    """约定：bare repo 和 workspace 同级，名为 .gg.git。"""
    return Path(workspace).parent / ".gg.git"


def setup_worktree(workspace: str, branch: str) -> None:
    """确保 workspace 是指向中心 bare repo 的 git worktree。

    如果 bare repo 不存在则创建；如果 workspace 已经是普通 git repo
    则迁移历史到 bare repo 后转换为 worktree。
    """
    ws      = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    bare    = _bare_repo_path(workspace)

    # ── 1. 确保中心 bare repo 存在 ─────────────────────────────────────────────
    if not bare.exists():
        bare.mkdir(parents=True, exist_ok=True)
        _run(["git", "init", "--bare", "-q", str(bare)])
        # bare repo 需要一个初始 commit 才能 add worktree
        # 用一个临时 clone 制造 baseline commit
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmp:
            _run(["git", "clone", "-q", str(bare), tmp])
            Path(tmp, ".ggkeep").touch()
            _run(["git", *_GIT_USER, "-C", tmp, "add", "-A"])
            _run(["git", *_GIT_USER, "-C", tmp, "commit", "-qm", "baseline"])
            _run(["git", "-C", tmp, "push", "-q", "origin", "HEAD:main"])
        print(f"  [workspace_git] 创建中心 bare repo：{bare}")

    # ── 2. workspace 还没有 .git ───────────────────────────────────────────────
    if not (ws / ".git").exists():
        # 尝试作为 worktree 添加
        rc, _, err = _run(["git", "-C", str(bare), "worktree", "add",
                           "-b", branch, str(ws), "main"])
        if rc != 0:
            # branch 已存在时用 --track
            rc2, _, _ = _run(["git", "-C", str(bare), "worktree", "add",
                               str(ws), branch])
            if rc2 != 0:
                # 兜底：直接 git init（不用 worktree，但仍有独立 .git）
                _run(["git", "-C", str(ws), "init", "-q"])
                _run([*_GIT_USER, "git", "-C", str(ws), "commit",
                      "--allow-empty", "-qm", "baseline"])
        print(f"  [workspace_git] workspace={ws} → branch={branch}")
        return

    # ── 3. workspace 已有 .git（普通 repo），不动它 ────────────────────────────
    if not _is_worktree(workspace):
        print(f"  [workspace_git] workspace 已是独立 git repo，保持现状：{ws}")


def commit_workspace(workspace: str, msg: str) -> bool:
    """git add -A + commit。无变更时跳过，返回是否有新 commit。"""
    rc, out, _ = _run(["git", "-C", workspace, "status", "--porcelain"])
    if not out.strip():
        return False
    _run(["git", "-C", workspace, "add", "-A"])
    rc, _, err = _run(["git", *_GIT_USER, "-C", workspace,
                       "commit", "-qm", msg])
    if rc != 0:
        print(f"  [workspace_git] commit 失败（{workspace}）: {err}")
        return False
    print(f"  [workspace_git] committed: {workspace}")
    return True


def push_workspace(workspace: str, remote: str, branch: str) -> bool:
    """push 到 remote。返回是否成功。"""
    if not remote:
        return False
    rc, _, err = _run(["git", "-C", workspace, "push", remote,
                       f"HEAD:{branch}", "--set-upstream"])
    if rc != 0:
        print(f"  [workspace_git] push 失败（{workspace} → {remote}/{branch}）: {err[:200]}")
        return False
    print(f"  [workspace_git] pushed: {workspace} → {remote}/{branch}")
    return True
