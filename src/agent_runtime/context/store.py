"""
Context — ContextStore

三层上下文加载：L0（条件加载）/ L1（task 入口）/ L2（动态检索）。(DESIGN.md §十)

v1 实现：L1 基于文件系统，L2 暂时用 cold store 扫描。
LanceDB 向量索引在 L1 稳定后再接入。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_runtime.contracts import GoalId, ProjectId, SessionId, ScopeLevel
from agent_runtime.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class ContextStore:

    def __init__(self, storage: StorageBackend, project_id: ProjectId) -> None:
        self._s = storage
        self._project_id = project_id

    # ------------------------------------------------------------------
    # L0 — 身份/角色（条件加载，只有需要身份感知的 worker 才加载）
    # ------------------------------------------------------------------

    def load_l0(self, role: Optional[str] = None) -> Dict[str, Any]:
        """~50 tokens。只有 role 非 None 时才加载。"""
        if role is None:
            return {}
        path = f".context/{self._project_id}/l0/{role}.json"
        raw = self._s.read(path)
        if raw is None:
            return {}
        return json.loads(raw)

    # ------------------------------------------------------------------
    # L1 — task 入口上下文（依赖 session_id + parent_goal_id，不依赖 task_id）
    # D9-b 决策：L1 加载不依赖 task_id
    # ------------------------------------------------------------------

    def load_l1(
        self,
        session_id: SessionId,
        parent_goal_id: Optional[GoalId],
    ) -> Dict[str, Any]:
        """
        加载当前 GOAL + 父 GOAL + 直接依赖的上游 OBSERVE 摘要。
        ~100-200 tokens。
        """
        result: Dict[str, Any] = {"session_id": session_id}

        # 父 GOAL 上下文
        if parent_goal_id:
            parent_ctx = self._read_goal_context(parent_goal_id)
            if parent_ctx:
                result["parent_goal"] = parent_ctx

        # 当前 session 的 active context
        session_path = f".context/{self._project_id}/sessions/{session_id}/active.json"
        raw = self._s.read(session_path)
        if raw:
            result["session_context"] = json.loads(raw)

        return result

    def refresh_l1(
        self,
        session_id: SessionId,
        parent_goal_id: Optional[GoalId],
    ) -> Dict[str, Any]:
        """replan 时或子树完成时刷新 L1。"""
        return self.load_l1(session_id, parent_goal_id)

    # ------------------------------------------------------------------
    # L2 — 动态检索（按需，worker 主动调用或 task 开始时自动检索一次）
    # ------------------------------------------------------------------

    def search_l2(
        self,
        query: str,
        scope: ScopeLevel = ScopeLevel.PROJECT,
        limit: int = 10,
        retrieved_at_ref: Optional[str] = None,  # 新鲜度检查用
    ) -> List[Dict[str, Any]]:
        """
        v1: 扫描 hot/ 目录，简单文本匹配。
        v2: 接入 LanceDB 向量 + BM25 混合检索。
        """
        results = []
        prefix = f".context/{self._project_id}/hot/"
        query_lower = query.lower()

        for path in self._s.list(prefix):
            if len(results) >= limit:
                break
            raw = self._s.read(path)
            if raw is None:
                continue
            try:
                entry = json.loads(raw)
                content = str(entry.get("content", "")).lower()
                if query_lower in content:
                    entry["_path"] = path
                    results.append(entry)
            except (json.JSONDecodeError, Exception):
                continue

        return results

    # ------------------------------------------------------------------
    # 写入端
    # ------------------------------------------------------------------

    def write_observe_summary(
        self,
        task_id: str,
        goal_id: GoalId,
        summary: str,
        scope: ScopeLevel = ScopeLevel.TASK,
        resource_versions: Optional[Dict[str, str]] = None,
        session_id: Optional[str] = None,  # #6: session_id 参数（直接接口，无需 inspect）
    ) -> None:
        """
        task 完成后写入 OBSERVE 摘要到热层。
        OBSERVE 归并规范：每层只传摘要，不传原文。(ADR-001 结论)
        """
        entry_id = str(uuid.uuid4())
        entry = {
            "id": entry_id,
            "task_id": task_id,
            "goal_id": goal_id,
            "content": summary,
            "scope": scope.value,
            "resource_versions": resource_versions or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if session_id is not None:
            entry["session_id"] = session_id
        path = f".context/{self._project_id}/hot/{entry_id}.json"
        self._s.write_atomic(path, json.dumps(entry, ensure_ascii=False).encode())

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _read_goal_context(self, goal_id: GoalId) -> Optional[Dict[str, Any]]:
        path = f".context/{self._project_id}/goals/{goal_id}.json"
        raw = self._s.read(path)
        if raw is None:
            return None
        return json.loads(raw)
