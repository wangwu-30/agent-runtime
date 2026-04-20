import pytest
from agent_runtime.contracts import (
    Goal, ActionRequest, EvalResult, FailureType,
    TaskState, LEGAL_TRANSITIONS,
)


def make_goal(**kwargs):
    defaults = dict(
        id="goal-001",
        content="Write hello.txt",
        success_criteria="File exists",
        parent_goal_id=None,
        root_goal_id="goal-001",
        session_id="session-001",
    )
    defaults.update(kwargs)
    return Goal(**defaults)


def make_action(**kwargs):
    defaults = dict(
        id="act-001",
        tool_id="write_file",
        params={"path": "out.txt", "content": "hi"},
        goal_ref="goal-001",
        span_id="span-001",
        resources=[],
    )
    defaults.update(kwargs)
    return ActionRequest(**defaults)


class TestGoal:
    def test_create(self):
        g = make_goal()
        assert g.id == "goal-001"
        assert g.content == "Write hello.txt"
        assert g.success_criteria == "File exists"

    def test_no_success_criteria(self):
        g = make_goal(success_criteria=None)
        assert g.success_criteria is None

    def test_frozen(self):
        g = make_goal()
        with pytest.raises((AttributeError, TypeError)):
            g.content = "changed"  # type: ignore


class TestActionRequest:
    def test_create(self):
        a = make_action()
        assert a.tool_id == "write_file"
        assert a.params["path"] == "out.txt"

    def test_frozen(self):
        a = make_action()
        with pytest.raises((AttributeError, TypeError)):
            a.tool_id = "other"  # type: ignore


class TestEvalResult:
    def test_satisfied(self):
        r = EvalResult(goal_satisfied=True, failure_type=None, confidence=0.9, rationale="ok")
        assert r.goal_satisfied is True

    def test_failed_requires_failure_type(self):
        with pytest.raises(ValueError, match="failure_type is required"):
            EvalResult(goal_satisfied=False, failure_type=None, confidence=0.0, rationale="bad")

    def test_failed_with_type(self):
        r = EvalResult(goal_satisfied=False, failure_type=FailureType.O2C, confidence=0.1, rationale="err")
        assert r.failure_type == FailureType.O2C


class TestLegalTransitions:
    def test_all_states_covered(self):
        for state in TaskState:
            assert state in LEGAL_TRANSITIONS

    def test_terminal_states_empty(self):
        assert LEGAL_TRANSITIONS[TaskState.DONE] == frozenset()
        assert LEGAL_TRANSITIONS[TaskState.DEAD] == frozenset()
        assert LEGAL_TRANSITIONS[TaskState.CANCELLED] == frozenset()
