"""
Runner — LocalRunner + WorktreeManager

本地执行环境。每个 task 一个 git worktree，完全隔离。(DESIGN.md §7.2, ADR-002)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from agent_runtime.contracts import TaskId

logger = logging.getLogger(__name__)


class WorktreeManager:
    """
    管理 git worktree 的生命周期。

    每个 task 创建独立 worktree：
      task 开始 → create_worktree()
      judge 通过 → merge_and_cleanup()
      task 失败/replan → discard()
    """

    def __init__(self, repo_root: str | Path, use_git: bool = True) -> None:
        self._repo = Path(repo_root)
        self._use_git = use_git  # P0-4: 支持非 git 模式
        self._worktrees: dict[TaskId, Path] = {}

    def create(self, task_id: TaskId, base_branch: str = "main") -> Path:
        """创建 worktree，返回 worktree 路径。"""
        if not self._use_git:
            # P0-4: 非 git 模式：用 tempfile 创建临时目录
            wt_path = Path(tempfile.mkdtemp(prefix=f"agent_task_{task_id}_"))
            self._worktrees[task_id] = wt_path
            logger.info("Created temp workspace for task %s at %s (no-git mode)", task_id, wt_path)
            return wt_path
        wt_path = self._repo / ".worktrees" / task_id
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"task/{task_id}"
        self._git(["worktree", "add", "-b", branch, str(wt_path), base_branch])
        self._worktrees[task_id] = wt_path
        logger.info("Created worktree for task %s at %s", task_id, wt_path)
        return wt_path

    def snapshot(self, task_id: TaskId) -> Optional[str]:
        """在 worktree 内打快照，返回 commit hash。非 git 模式返回 None。"""
        if not self._use_git:
            return None
        wt = self._worktrees.get(task_id)
        if wt is None:
            return None
        result = self._git_in(wt, ["rev-parse", "HEAD"])
        return result.strip() if result else None

    def reset_to_snapshot(self, task_id: TaskId, commit_hash: str) -> None:
        """回滚到快照。(DESIGN.md §8.1 Layer A) 非 git 模式为空操作。"""
        if not self._use_git:
            return
        wt = self._worktrees.get(task_id)
        if wt is None:
            raise KeyError(f"No worktree for task {task_id}")
        self._git_in(wt, ["reset", "--hard", commit_hash])
        logger.info("Reset worktree for task %s to %s", task_id, commit_hash)

    def merge_if_valid(self, task_id: TaskId, target_branch: str = "main") -> None:
        """
        judge 通过后 merge 到主线，这是 DONE 状态的最后一步。
        merge 发生在 VALIDATING 完成之后。(DESIGN.md §8.1 Layer A)
        非 git 模式：只做 cleanup。
        """
        if not self._use_git:
            self.discard(task_id)
            return
        wt = self._worktrees.get(task_id)
        if wt is None:
            raise KeyError(f"No worktree for task {task_id}")
        branch = f"task/{task_id}"
        # 在主仓库 merge
        self._git(["merge", "--no-ff", branch, "-m", f"task({task_id}): merge"])
        logger.info("Merged task %s into %s", task_id, target_branch)
        self.discard(task_id)

    def discard(self, task_id: TaskId) -> None:
        """丢弃 worktree（失败/replan）。"""
        wt = self._worktrees.pop(task_id, None)
        if wt is None:
            return
        if not self._use_git:
            # P0-4: 非 git 模式：shutil.rmtree 删除临时目录
            try:
                shutil.rmtree(wt, ignore_errors=True)
            except Exception as e:
                logger.warning("Failed to remove temp workspace for task %s: %s", task_id, e)
            return
        branch = f"task/{task_id}"
        try:
            self._git(["worktree", "remove", "--force", str(wt)])
            self._git(["branch", "-D", branch])
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to discard worktree for task %s: %s", task_id, e)

    def get_path(self, task_id: TaskId) -> Optional[Path]:
        return self._worktrees.get(task_id)

    def _git(self, args: list[str]) -> str:
        result = subprocess.run(
            ["git"] + args,
            cwd=self._repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def _git_in(self, wt: Path, args: list[str]) -> str:
        result = subprocess.run(
            ["git"] + args,
            cwd=wt,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout


class LocalRunner:
    """
    本地 runner。封装 worktree 管理 + 工具执行。

    进程隔离：v1 使用 subprocess，v2 可换 virtualenv/nix shell。
    """

    def __init__(self, worktree_manager: WorktreeManager) -> None:
        self._wm = worktree_manager

    def prepare_workspace(self, task_id: TaskId, base_branch: str = "main") -> Path:
        return self._wm.create(task_id, base_branch)

    def snapshot(self, task_id: TaskId) -> Optional[str]:
        return self._wm.snapshot(task_id)

    def reset_to_snapshot(self, task_id: TaskId, commit_hash: str) -> None:
        self._wm.reset_to_snapshot(task_id, commit_hash)

    def merge_if_valid(self, task_id: TaskId) -> None:
        self._wm.merge_if_valid(task_id)

    def discard(self, task_id: TaskId) -> None:
        self._wm.discard(task_id)

    def get_workspace_path(self, task_id: TaskId) -> Optional[Path]:
        return self._wm.get_path(task_id)
