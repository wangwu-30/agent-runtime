"""
Kernel — TaskStateMachine

强制执行合法状态转换。非法转换抛出异常，不静默忽略。
"""

from __future__ import annotations

from agent_runtime.contracts import TaskState, LEGAL_TRANSITIONS


class IllegalTransitionError(Exception):
    def __init__(self, task_id: str, from_state: TaskState, to_state: TaskState) -> None:
        super().__init__(
            f"Task {task_id}: illegal transition {from_state.value} → {to_state.value}"
        )
        self.task_id = task_id
        self.from_state = from_state
        self.to_state = to_state


class TaskStateMachine:

    @staticmethod
    def transition(task_id: str, current: TaskState, target: TaskState) -> TaskState:
        """验证并执行状态转换，返回新状态。"""
        allowed = LEGAL_TRANSITIONS.get(current, frozenset())
        if target not in allowed:
            raise IllegalTransitionError(task_id, current, target)
        return target

    @staticmethod
    def can_transition(current: TaskState, target: TaskState) -> bool:
        return target in LEGAL_TRANSITIONS.get(current, frozenset())

    @staticmethod
    def is_terminal(state: TaskState) -> bool:
        return not LEGAL_TRANSITIONS.get(state, frozenset())

    @staticmethod
    def is_waiting(state: TaskState) -> bool:
        """SUSPENDED / HITL_WAITING / CLARIFYING — 不发 heartbeat，不占用 worker。
        # TODO(v2/clarify): CLARIFYING 的唯一合法入口是 VALIDATING → CLARIFYING（O2a 路径）。
        # CLARIFYING 状态不持锁（holds_locks(CLARIFYING) = False）。
        # CLARIFYING → RUNNING 时需要重新按全局字典序获取资源锁（ADR-011）。
        """
        return state in {TaskState.SUSPENDED, TaskState.HITL_WAITING, TaskState.CLARIFYING}

    @staticmethod
    def holds_locks(state: TaskState) -> bool:
        """持锁状态。(ADR-013 状态-锁矩阵)"""
        return state in {TaskState.RUNNING, TaskState.HITL_WAITING}
