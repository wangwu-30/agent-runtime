"""
Kernel — Scheduler

工作窃取 + 优先级队列调度器。(ADR-010)

goroutine 模型：task 和 worker 不强绑定。
task 在 yield 点释放 worker，任意空闲 worker 可以接手。(ADR-006)

v1 实现：单一全局优先级队列 + heartbeat 故障恢复。
工作窃取（跨 worker 队列窃取）推迟到 v2。
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from agent_runtime.contracts import TaskId, TaskState, WorkerId
from agent_runtime.kernel.state_machine import TaskStateMachine

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 10
HEARTBEAT_TIMEOUT_MULTIPLIER = 3  # 3× interval = 30s 检测到故障


_seq = 0  # 模块级序列号，稳定 tie-breaker (P2-1)


@dataclass(order=True)
class _QueueEntry:
    priority: float         # 越小越优先（负优先级）
    seq: int = field(compare=True)   # 稳定 tie-breaker (P2-1)
    created_at: datetime = field(compare=False)
    task_id: TaskId = field(compare=False)


class Scheduler:
    """
    全局优先级队列调度器。

    优先级计算：P = -base_priority × (decay_factor ^ depth)
    decay_factor < 1，深层 task 优先级递减，防止无限递归 task 饿死根 GOAL。
    """

    DECAY_FACTOR = 0.9

    def __init__(self) -> None:
        self._heap: List[_QueueEntry] = []
        # #9: 删除 _task_states，Task.state 是唯一 source of truth
        self._task_priorities: Dict[TaskId, float] = {}
        self._heartbeats: Dict[TaskId, datetime] = {}  # RUNNING task 的最后心跳
        self._waiting: Set[TaskId] = set()              # SUSPENDED/HITL_WAITING task
        self._cancelled: Set[TaskId] = set()            # 用户#1: 终态 task，dequeue 时跳过
        self._hitl_waiting: Set[TaskId] = set()         # 用户#2: 独立追踪 HITL_WAITING task
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Enqueue / Dequeue
    # ------------------------------------------------------------------

    async def enqueue(self, task_id: TaskId, priority: float, state: TaskState = TaskState.PENDING) -> None:
        # #9: 不再写 _task_states，Task.state 是唯一 source of truth
        global _seq
        async with self._lock:
            self._task_priorities[task_id] = priority
            if state == TaskState.PENDING:
                _seq += 1
                heapq.heappush(self._heap, _QueueEntry(
                    priority=-priority,
                    seq=_seq,
                    created_at=datetime.now(timezone.utc),
                    task_id=task_id,
                ))

    async def dequeue(self) -> Optional[TaskId]:
        """取出优先级最高的 PENDING task，返回 task_id 或 None。
        #9: 不再写 _task_states，调用方负责通过 TaskStateMachine 转换状态。
        heartbeat 在此处预先记录，供超时检测使用。
        用户#1: 跳过已取消/终态的 task。
        """
        async with self._lock:
            while self._heap:
                entry = heapq.heappop(self._heap)
                if entry.task_id in self._cancelled:
                    self._cancelled.discard(entry.task_id)  # 清理
                    continue
                # #9: 不再读 _task_states；队列中的条目是 PENDING，直接返回
                self._heartbeats[entry.task_id] = datetime.now(timezone.utc)
                return entry.task_id
            return None

    # ------------------------------------------------------------------
    # Task lifecycle events
    # ------------------------------------------------------------------

    async def on_yield(self, task_id: TaskId, new_state: TaskState, resume_condition: Any = None) -> None:
        """task 主动 yield，释放 worker。(goroutine 模型)
        #4: SUSPENDED 时 resume_condition 不能为 None。
        #9: 不再写 _task_states。
        用户#2: HITL_WAITING 时加入 _hitl_waiting。
        """
        if new_state == TaskState.SUSPENDED and resume_condition is None:
            raise ValueError(f"Task {task_id}: resume_condition is required when entering SUSPENDED")
        async with self._lock:
            self._heartbeats.pop(task_id, None)
            if TaskStateMachine.is_waiting(new_state):
                self._waiting.add(task_id)
            if new_state == TaskState.HITL_WAITING:
                self._hitl_waiting.add(task_id)

    async def on_resume(self, task_id: TaskId) -> None:
        """恢复条件满足，task 重新入队。#9: 不再写 _task_states。"""
        global _seq
        async with self._lock:
            self._waiting.discard(task_id)
            priority = self._task_priorities.get(task_id, 0.0)
            _seq += 1
            heapq.heappush(self._heap, _QueueEntry(
                priority=-priority,
                seq=_seq,
                created_at=datetime.now(timezone.utc),
                task_id=task_id,
            ))

    async def on_complete(self, task_id: TaskId, final_state: TaskState) -> None:
        """#9: 不再写 _task_states。Task.state 是唯一 source of truth。
        用户#1: 终态 task 加入 _cancelled，防止 dequeue 取出。
        用户#2: 从 _hitl_waiting 移除。
        """
        async with self._lock:
            self._heartbeats.pop(task_id, None)
            self._waiting.discard(task_id)
            self._hitl_waiting.discard(task_id)
            if final_state in {TaskState.FAILED, TaskState.DEAD, TaskState.CANCELLED}:
                self._cancelled.add(task_id)

    # ------------------------------------------------------------------
    # Heartbeat（RUNNING task 定期发送，SUSPENDED/HITL_WAITING 豁免）
    # ------------------------------------------------------------------

    async def heartbeat(self, task_id: TaskId) -> None:
        """worker 代表 RUNNING task 发送心跳。SUSPENDED 不调用此方法。
        #9: 不再读 _task_states；只要 task 在发心跳就更新时间戳。
        """
        async with self._lock:
            self._heartbeats[task_id] = datetime.now(timezone.utc)

    async def scheduler_heartbeat_for_hitl(self, task_id: TaskId) -> bool:
        """
        scheduler 为 HITL_WAITING task 代续租锁。(ADR-014)
        返回 False 表示续租失败（scheduler 检测到异常），需要 fail-closed。
        #9: 不再读 _task_states；由调用方保证只对 HITL_WAITING task 调用。
        用户#2: 使用 _hitl_waiting 判断，而非 _heartbeats（on_yield 时已 pop heartbeat）。
        """
        # v1: 简单记录时间戳，实际锁续租在 LockManager 中实现
        async with self._lock:
            if task_id in self._hitl_waiting:
                self._heartbeats[task_id] = datetime.now(timezone.utc)
                return True
        return False

    async def check_timeouts(self) -> List[TaskId]:
        """
        检测 heartbeat 超时的 RUNNING task，返回需要重新入队的 task_id 列表。
        SUSPENDED / HITL_WAITING task 豁免超时检测。(ADR-013)
        #9: 不再读/写 _task_states。_heartbeats 里的 task 一定是 RUNNING 的，
        因为只有 RUNNING task 发 heartbeat；on_yield/on_complete 时会清除 heartbeat。
        """
        global _seq
        threshold = datetime.now(timezone.utc) - timedelta(
            seconds=HEARTBEAT_INTERVAL_S * HEARTBEAT_TIMEOUT_MULTIPLIER
        )
        timed_out = []
        async with self._lock:
            for task_id, last_hb in list(self._heartbeats.items()):
                if task_id in self._waiting:
                    # SUSPENDED/HITL_WAITING 豁免（不应出现在 _heartbeats，防御性跳过）
                    continue
                if last_hb < threshold:
                    logger.warning("Task %s heartbeat timeout, requeueing", task_id)
                    del self._heartbeats[task_id]
                    priority = self._task_priorities.get(task_id, 0.0)
                    _seq += 1
                    heapq.heappush(self._heap, _QueueEntry(
                        priority=-priority,
                        seq=_seq,
                        created_at=datetime.now(timezone.utc),
                        task_id=task_id,
                    ))
                    timed_out.append(task_id)
        return timed_out

    @staticmethod
    def compute_priority(base_priority: float, depth: int) -> float:
        """P = base_priority × decay_factor^depth。深层 task 优先级递减。"""
        return base_priority * (Scheduler.DECAY_FACTOR ** depth)
