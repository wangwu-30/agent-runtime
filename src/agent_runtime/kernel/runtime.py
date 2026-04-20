"""
Kernel — RuntimeKernel

系统入口。把所有子系统组装起来，提供 handle_message() 接口。

v1 垂直切片：单叶子节点任务，真实文件写入，不涉及 DAG/replan/并行。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from agent_runtime.contracts import (
    ActionAttempt, ActionRequest, AttemptStatus, BackendType,
    EvalResult, FailureType, Goal, GoalId, ObserveEvent,
    ProjectId, SessionId, Span, SpanStatus, Task, TaskId, TaskState,
    TraceId, SpanId,
)
from agent_runtime.context.store import ContextStore
from agent_runtime.kernel.scheduler import Scheduler
from agent_runtime.kernel.state_machine import TaskStateMachine
from agent_runtime.runner.local import LocalRunner
from agent_runtime.storage.base import StorageBackend
from agent_runtime.tools.registry import PermissionEngine, ToolRegistry
from agent_runtime.trace.store import TraceStore

logger = logging.getLogger(__name__)


class RuntimeKernel:
    """
    系统入口。组装所有子系统。

    执行顺序（DESIGN.md §6.3）：
      message → session_id → L0/L1 加载 → clarifier → task_id → executor
      → L2 检索 → ACTION → evaluator → context 写入 → 学习层（异步）
    """

    def __init__(
        self,
        storage: StorageBackend,
        registry: ToolRegistry,
        runner: LocalRunner,
        project_id: ProjectId,
    ) -> None:
        self._storage = storage
        self._trace = TraceStore(storage)
        self._context = ContextStore(storage, project_id)
        self._registry = registry
        self._permission = PermissionEngine(registry, storage.backend_type)
        self._runner = runner
        self._scheduler = Scheduler()
        self._project_id = project_id
        self._sm = TaskStateMachine()
        self._planner: Optional[Callable] = None  # P0-2: 注入点，由调用方提供

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def handle_message(
        self,
        goal_content: str,
        session_key: str,
        success_criteria: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        处理用户消息：创建 task 并入队。
        执行逻辑在 _execute_task() 中，由调用方在 dequeue 后调用。
        v1 保持同步，但状态转换必须通过 TaskStateMachine.transition()。（问题1）
        """
        # 1. 分配 session_id
        session_id = self._get_or_create_session(session_key)

        # 2. L1 加载（不依赖 task_id，D9-b）
        l1 = self._context.load_l1(session_id, parent_goal_id=None)

        # 3. 构建 Goal（clarifier 在此处澄清，v1 直通）
        goal_id = _new_id("goal")
        trace_id = _new_id("trace")
        goal = Goal(
            id=goal_id,
            content=goal_content,
            success_criteria=success_criteria,
            parent_goal_id=None,
            root_goal_id=goal_id,
            session_id=session_id,
        )
        goal = self._clarify(goal, l1)

        # 4. 分配 task_id，创建 task
        task_id = _new_id("task")
        # span_id 是纯 span id，trace_id 单独存（问题2）
        span_id = _new_id("span")
        task = Task(
            id=task_id,
            goal=goal,
            state=TaskState.PENDING,
            session_id=session_id,
            trace_id=trace_id,
            span_id=span_id,
            parent_task_id=None,
            project_id=self._project_id,
            priority=Scheduler.compute_priority(1.0, depth=0),
        )

        # 5. 调度：只负责入队，不直接执行（问题1）
        await self._scheduler.enqueue(task_id, task.priority)

        # 6. 执行（v1 同步：dequeue 后立即执行；v2 改为 worker loop）
        dequeued = await self._scheduler.dequeue()
        if dequeued != task_id:
            # v1 简化：理论上队列中只有一个 task，dequeue 应该返回相同 task_id
            logger.warning("Dequeued unexpected task %s, expected %s", dequeued, task_id)

        return await self._execute_task(task)

    # ------------------------------------------------------------------
    # Internal execution
    # ------------------------------------------------------------------

    async def _execute_task(self, task: Task) -> Dict[str, Any]:
        task_id = task.id
        trace_id = task.trace_id
        span_id = task.span_id  # 纯 span id（问题2）

        # 通过状态机转换到 RUNNING（问题1）
        task.state = self._sm.transition(task_id, task.state, TaskState.RUNNING)
        await self._scheduler.heartbeat(task_id)

        # 先开始 span，再 prepare_workspace（#10：确保 except 里 close_span 时 span 已存在）
        span = Span(
            id=span_id,
            trace_id=trace_id,
            parent_span_id=None,
            task_id=task_id,
            goal=task.goal,
            worker_id="local-worker-0",
            started_at=datetime.now(timezone.utc),
        )
        self._trace.begin_span(span)

        try:
            # 准备 workspace（在 begin_span 之后、try 块内，#10/#P1-1）
            workspace = self._runner.prepare_workspace(task_id)
            snapshot_hash = self._runner.snapshot(task_id)
            if snapshot_hash:
                self._trace.write_snapshot_ref(trace_id, span_id, snapshot_hash)

            # executor：v1 叶子节点，直接规划一个 ACTION
            action = self._plan_leaf_action(task.goal, workspace)

            # 权限检查
            decision = self._permission.check(action)
            if not decision.allowed:
                if decision.requires_hitl:
                    # v1 暂不实现 HITL 流程，直接失败
                    raise NotImplementedError(f"HITL required: {decision.reason}")
                raise PermissionError(decision.reason)

            # 写入 action_attempt PENDING（ADR-016 T2-T4 保护）
            # status 只写入 trace 文件，不在 ActionAttempt 对象上（#2）
            attempt = ActionAttempt(
                id=_new_id("attempt"),
                span_id=span_id,
                action_request=action,
            )
            # TraceStore 方法显式接收 trace_id 和 span_id（问题2）
            self._trace.append_action_attempt(trace_id, span_id, attempt)

            # 执行 ACTION
            raw_result = self._run_action(action, workspace)

            # 写入 COMMITTED
            self._trace.commit_action_attempt(trace_id, span_id, attempt)

            # 构建 OBSERVE（backend 传 BackendType 实例，不是 .value，问题4）
            observe = ObserveEvent(
                action_id=action.id,
                span_id=span_id,
                raw_output=raw_result,
                # TODO(v2): 填充 resource_versions（资源路径 → git commit hash）
                # 用于 SUSPENDED world-state generation check（ADR-013）
                # 当前为空导致 world-state check 永远通过，v1 接受此限制
                resource_versions={},
                observed_at=datetime.now(timezone.utc),
                backend=self._storage.backend_type,  # BackendType 实例（问题4）
            )
            self._trace.write_observe(trace_id, span_id, observe)

            # evaluator
            task.state = self._sm.transition(task_id, task.state, TaskState.VALIDATING)
            eval_result = self._evaluate(task.goal, observe)
            self._trace.append_eval(trace_id, span_id, eval_result)

            if not eval_result.goal_satisfied:
                # v1 不实现 replan，直接失败
                task.state = self._sm.transition(task_id, task.state, TaskState.FAILED)
                self._trace.close_span(trace_id, span_id, SpanStatus.FAILED)
                await self._scheduler.on_complete(task_id, TaskState.FAILED)
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "failure_type": eval_result.failure_type.value if eval_result.failure_type else None,
                    "rationale": eval_result.rationale,
                }

            # DONE：merge worktree，这是 DONE 的最后一步（DESIGN.md §8.1 Layer A）
            self._runner.merge_if_valid(task_id)
            task.state = self._sm.transition(task_id, task.state, TaskState.DONE)
            self._trace.close_span(trace_id, span_id, SpanStatus.DONE)

            # 写入 context 摘要（问题6：更新 session active context）
            session_id = task.session_id
            self._write_observe_summary_with_session(
                task_id=task_id,
                goal_id=task.goal.id,
                summary=str(raw_result)[:500],
                session_id=session_id,
            )

            await self._scheduler.on_complete(task_id, TaskState.DONE)
            return {"task_id": task_id, "status": "done", "result": raw_result}

        except Exception as e:
            logger.exception("Task %s failed: %s", task_id, e)
            self._runner.discard(task_id)
            if not TaskStateMachine.is_terminal(task.state):
                try:
                    task.state = self._sm.transition(task_id, task.state, TaskState.FAILED)
                except Exception:
                    task.state = TaskState.FAILED
            self._trace.close_span(trace_id, span_id, SpanStatus.FAILED)
            await self._scheduler.on_complete(task_id, TaskState.FAILED)
            raise

    # ------------------------------------------------------------------
    # Roles（v1 最简实现，v2 替换为 LLM 驱动）
    # ------------------------------------------------------------------

    def set_planner(self, fn: Callable[[Goal, Any], ActionRequest]) -> None:
        """注入 planner：从 Goal 映射到 ActionRequest。v1 由调用方提供，v2 改为 LLM 驱动。(P0-2)"""
        self._planner = fn

    def _clarify(self, goal: Goal, l1: Dict[str, Any]) -> Goal:
        """
        clarifier：v1 直通（success_criteria 已有则不澄清）。
        v2：调用 LLM 判断是否需要澄清。
        """
        if goal.success_criteria is None:
            logger.warning("Goal %s has no success_criteria, proceeding without clarification", goal.id)
        return goal

    def _plan_leaf_action(self, goal: Goal, workspace) -> ActionRequest:
        """
        executor：v1 通过注入的 planner 函数规划。
        v2：调用 LLM 规划 ACTION 序列。
        # TODO(v2): CLARIFYING → RUNNING 时需要重新获取资源锁
        # CLARIFYING 期间锁已释放（holds_locks(CLARIFYING) = False）
        # 恢复执行前必须重新按全局字典序获取所需锁
        """
        if self._planner is None:
            raise NotImplementedError(
                "No planner registered. Call set_planner() before handle_message(). "
                "Example: kernel.set_planner(lambda goal, ws: ActionRequest(...))"
            )
        return self._planner(goal, workspace)

    def _run_action(self, action: ActionRequest, workspace) -> Any:
        """
        action_runner：通过 ToolRegistry 执行工具。(P0-3)
        """
        ws_path = str(workspace) if workspace else None
        return self._registry.execute(action.tool_id, action.params, workspace_path=ws_path)

    def _evaluate(self, goal: Goal, observe: ObserveEvent) -> EvalResult:
        """
        evaluator：v1 规则判断，v2 独立 judge 模型。
        """
        # 默认：有输出就算成功（v1 占位）
        satisfied = observe.raw_output is not None
        return EvalResult(
            goal_satisfied=satisfied,
            failure_type=None if satisfied else FailureType.O2C,
            confidence=0.5,
            rationale="v1 placeholder evaluator",
        )

    # ------------------------------------------------------------------
    # Context helpers（问题6：在 runtime 层处理 session active context 更新）
    # ------------------------------------------------------------------

    def _write_observe_summary_with_session(
        self,
        task_id: str,
        goal_id: str,
        summary: str,
        session_id: SessionId,
    ) -> None:
        """
        写入 OBSERVE 摘要，并更新 session 的 active context。(问题6, #6)
        ContextStore.write_observe_summary 接受 session_id 参数，直接调用。
        """
        self._context.write_observe_summary(
            task_id=task_id,
            goal_id=goal_id,
            summary=summary,
            session_id=session_id,
        )
        # 同步更新 sessions/{session_id}/active.json（问题6）
        self._update_session_active_context(session_id, goal_id)

    def _update_session_active_context(self, session_id: SessionId, goal_id: str) -> None:
        """追加摘要引用到 sessions/{session_id}/active.json。(问题6)"""
        active_path = f".context/{self._project_id}/sessions/{session_id}/active.json"
        raw = self._storage.read(active_path)
        if raw:
            data = json.loads(raw)
        else:
            data = {"session_id": session_id, "summaries": []}
        summaries = data.get("summaries", [])
        summaries.append({"goal_id": goal_id, "at": datetime.now(timezone.utc).isoformat()})
        data["summaries"] = summaries
        self._storage.write_atomic(
            active_path,
            json.dumps(data, ensure_ascii=False).encode(),
        )

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(self, session_key: str) -> SessionId:
        path = f".context/{self._project_id}/sessions/{session_key}/id.txt"
        raw = self._storage.read(path)
        if raw:
            return raw.decode().strip()
        session_id = _new_id("session")
        self._storage.write_atomic(path, session_id.encode())
        # 初始化 session 的 active context（问题6）
        active_path = f".context/{self._project_id}/sessions/{session_id}/active.json"
        if not self._storage.exists(active_path):
            self._storage.write_atomic(
                active_path,
                json.dumps(
                    {"session_id": session_id, "summaries": []},
                    ensure_ascii=False,
                ).encode(),
            )
        return session_id


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
