import pytest
from agent_runtime.contracts import TaskState
from agent_runtime.kernel.state_machine import TaskStateMachine, IllegalTransitionError


class TestTransition:
    def test_pending_to_running(self):
        result = TaskStateMachine.transition("t1", TaskState.PENDING, TaskState.RUNNING)
        assert result == TaskState.RUNNING

    def test_running_to_validating(self):
        result = TaskStateMachine.transition("t1", TaskState.RUNNING, TaskState.VALIDATING)
        assert result == TaskState.VALIDATING

    def test_validating_to_done(self):
        result = TaskStateMachine.transition("t1", TaskState.VALIDATING, TaskState.DONE)
        assert result == TaskState.DONE

    def test_illegal_done_to_running(self):
        with pytest.raises(IllegalTransitionError) as exc_info:
            TaskStateMachine.transition("t1", TaskState.DONE, TaskState.RUNNING)
        assert exc_info.value.from_state == TaskState.DONE
        assert exc_info.value.to_state == TaskState.RUNNING

    def test_illegal_pending_to_done(self):
        with pytest.raises(IllegalTransitionError):
            TaskStateMachine.transition("t1", TaskState.PENDING, TaskState.DONE)


class TestPredicates:
    def test_is_terminal_done(self):
        assert TaskStateMachine.is_terminal(TaskState.DONE) is True

    def test_is_terminal_dead(self):
        assert TaskStateMachine.is_terminal(TaskState.DEAD) is True

    def test_is_terminal_cancelled(self):
        assert TaskStateMachine.is_terminal(TaskState.CANCELLED) is True

    def test_is_not_terminal_running(self):
        assert TaskStateMachine.is_terminal(TaskState.RUNNING) is False

    def test_is_waiting_suspended(self):
        assert TaskStateMachine.is_waiting(TaskState.SUSPENDED) is True

    def test_is_waiting_hitl(self):
        assert TaskStateMachine.is_waiting(TaskState.HITL_WAITING) is True

    def test_is_not_waiting_running(self):
        assert TaskStateMachine.is_waiting(TaskState.RUNNING) is False

    def test_holds_locks_running(self):
        assert TaskStateMachine.holds_locks(TaskState.RUNNING) is True

    def test_holds_locks_hitl(self):
        assert TaskStateMachine.holds_locks(TaskState.HITL_WAITING) is True

    def test_not_holds_locks_suspended(self):
        assert TaskStateMachine.holds_locks(TaskState.SUSPENDED) is False

    def test_can_transition(self):
        assert TaskStateMachine.can_transition(TaskState.PENDING, TaskState.RUNNING) is True
        assert TaskStateMachine.can_transition(TaskState.DONE, TaskState.RUNNING) is False
