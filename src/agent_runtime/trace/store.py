"""
Trace — TraceStore

append-only 执行记录。所有写入都是追加，不覆盖历史。(HC6, ADR-016)

文件布局：
  .trace/{trace_id}/{span_id}/
    goal.json              — immutable
    plan.json              — immutable（replan 时创建新 span）
    action_attempts.jsonl  — append-only，PENDING/COMMITTED（ADR-016 T2-T4 保护）
    observe.json           — immutable
    eval.jsonl             — 版本化追加
    children.jsonl         — 子 span 注册，append-only
    snapshot.ref           — git commit hash
    span_events.jsonl      — span 生命周期事件，append-only（问题8）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from agent_runtime.contracts import (
    ActionAttempt, AttemptStatus, EvalResult, Goal,
    ObserveEvent, Span, SpanId, SpanStatus, TaskId, TraceId,
)
from agent_runtime.storage.base import StorageBackend


class TraceStore:

    def __init__(self, storage: StorageBackend) -> None:
        storage.assert_trace_safe()
        self._s = storage

    # ------------------------------------------------------------------
    # Span lifecycle
    # ------------------------------------------------------------------

    def begin_span(self, span: Span) -> None:
        """开始 span：写入 immutable goal.json + meta.json，并追加 span_events.jsonl。(问题8)"""
        base = self._span_path(span.trace_id, span.id)
        self._s.write_atomic(f"{base}/goal.json", _dump(span.goal))
        # meta.json 只写一次（immutable），不在 close_span 中覆写（问题8）
        meta = {
            "span_id": span.id,
            "trace_id": span.trace_id,
            "parent_span_id": span.parent_span_id,
            "task_id": span.task_id,
            "worker_id": span.worker_id,
            "started_at": span.started_at.isoformat(),
        }
        self._s.write_atomic(f"{base}/meta.json", _dump(meta))
        # 追加 started 事件（问题8：begin_span 也写 span_events）
        self._s.append(f"{base}/span_events.jsonl", _line({
            "event": "started",
            "status": SpanStatus.RUNNING.value,
            "at": span.started_at.isoformat(),
        }))

    def write_plan(self, trace_id: TraceId, span_id: SpanId, plan: dict) -> None:
        """分解策略，immutable。"""
        path = f"{self._span_path(trace_id, span_id)}/plan.json"
        self._s.write_atomic(path, _dump(plan))

    def register_child(self, trace_id: TraceId, parent_span_id: SpanId, child_span_id: SpanId) -> None:
        path = f"{self._span_path(trace_id, parent_span_id)}/children.jsonl"
        self._s.append(path, _line({"child_span_id": child_span_id}))

    def mark_superseded(self, trace_id: TraceId, span_id: SpanId, superseded_by: SpanId) -> None:
        """replan 时标记旧 span，不删除。(HC6)"""
        base = self._span_path(trace_id, span_id)
        self._s.write_atomic(f"{base}/superseded.json", _dump({
            "superseded_by": superseded_by,
            "at": datetime.now(timezone.utc).isoformat(),
        }))
        # 同时追加 span_events（问题8）
        self._s.append(f"{base}/span_events.jsonl", _line({
            "event": "superseded",
            "status": SpanStatus.SUPERSEDED.value,
            "superseded_by": superseded_by,
            "at": datetime.now(timezone.utc).isoformat(),
        }))

    def write_snapshot_ref(self, trace_id: TraceId, span_id: SpanId, commit_hash: str) -> None:
        base = self._span_path(trace_id, span_id)
        self._s.write_atomic(f"{base}/snapshot.ref", commit_hash.encode())

    def close_span(self, trace_id: TraceId, span_id: SpanId, status: SpanStatus) -> None:
        """关闭 span：追加写入 span_events.jsonl，不覆写 meta.json。(问题8)"""
        base = self._span_path(trace_id, span_id)
        self._s.append(f"{base}/span_events.jsonl", _line({
            "event": "closed",
            "status": status.value,
            "at": datetime.now(timezone.utc).isoformat(),
        }))

    # ------------------------------------------------------------------
    # ACTION 执行记录（ADR-016 T2-T4 崩溃窗口保护）
    # ------------------------------------------------------------------

    def append_action_attempt(self, trace_id: TraceId, span_id: SpanId, attempt: ActionAttempt) -> None:
        """执行前写入 PENDING。必须在 ACTION 执行之前调用。

        写入完整 action 信息供恢复使用（问题3）。
        TraceStore 方法显式接收 trace_id 和 span_id（问题2），不从 span_id 字符串拆解。
        """
        path = f"{self._span_path(trace_id, span_id)}/action_attempts.jsonl"
        self._s.append(path, _line({
            "id": attempt.id,
            "action_id": attempt.action_request.id,
            "tool_id": attempt.action_request.tool_id,
            "params": attempt.action_request.params,                         # 新增（问题3）
            "resources": [r.path for r in attempt.action_request.resources], # 新增（问题3）
            "status": AttemptStatus.PENDING.value,
            "created_at": attempt.created_at.isoformat(),
        }))

    def commit_action_attempt(self, trace_id: TraceId, span_id: SpanId, attempt: ActionAttempt) -> None:
        """执行后写入 COMMITTED。
        TraceStore 方法显式接收 trace_id 和 span_id（问题2）。
        """
        path = f"{self._span_path(trace_id, span_id)}/action_attempts.jsonl"
        self._s.append(path, _line({
            "id": attempt.id,
            "status": AttemptStatus.COMMITTED.value,
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }))

    # ------------------------------------------------------------------
    # OBSERVE / EVAL
    # ------------------------------------------------------------------

    def write_observe(self, trace_id: TraceId, span_id: SpanId, observe: ObserveEvent) -> None:
        """immutable，写一次。写入 raw_output、span_id，backend 用 .value 序列化。(问题4)"""
        base = self._span_path(trace_id, span_id)
        # backend 可能是 BackendType enum 或 str（兼容旧版）
        backend_val = observe.backend.value if hasattr(observe.backend, "value") else observe.backend
        self._s.write_atomic(f"{base}/observe.json", _dump({
            "action_id": observe.action_id,
            "span_id": observe.span_id,                            # 新增（问题4）
            "raw_output": observe.raw_output,                      # 新增（问题4）
            "resource_versions": observe.resource_versions,
            "observed_at": observe.observed_at.isoformat(),
            "backend": backend_val,                                # .value 序列化（问题4）
        }))

    def append_eval(self, trace_id: TraceId, span_id: SpanId, result: EvalResult) -> None:
        """版本化追加，保留历史。"""
        path = f"{self._span_path(trace_id, span_id)}/eval.jsonl"
        self._s.append(path, _line({
            "goal_satisfied": result.goal_satisfied,
            "failure_type": result.failure_type.value if result.failure_type else None,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "evaluated_at": result.evaluated_at.isoformat(),
        }))

    # ------------------------------------------------------------------
    # Recovery helpers（ADR-016）
    # ------------------------------------------------------------------

    def get_pending_attempts(self, trace_id: TraceId, span_id: SpanId) -> List[dict]:
        """返回有 PENDING 但无对应 COMMITTED 的 attempt，用于崩溃恢复。
        TraceStore 方法显式接收 trace_id 和 span_id（问题2）。
        """
        path = f"{self._span_path(trace_id, span_id)}/action_attempts.jsonl"
        raw = self._s.read(path)
        if not raw:
            return []
        committed_ids = set()
        all_pending = {}
        for line in raw.decode().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry["status"] == AttemptStatus.COMMITTED.value:
                committed_ids.add(entry["id"])
            elif entry["status"] == AttemptStatus.PENDING.value:
                all_pending[entry["id"]] = entry
        return [v for k, v in all_pending.items() if k not in committed_ids]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _span_path(self, trace_id: TraceId, span_id: SpanId) -> str:
        """span 路径：显式接收 trace_id 和 span_id 两个参数（问题2）。
        删除了 _resolve_span()（问题2）。
        """
        return f".trace/{trace_id}/{span_id}"


def _dump(obj) -> bytes:
    return json.dumps(obj, default=str, ensure_ascii=False, indent=2).encode()


def _line(obj) -> bytes:
    return (json.dumps(obj, default=str, ensure_ascii=False) + "\n").encode()
