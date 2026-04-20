import pytest
from agent_runtime.contracts import (
    ActionRequest, ActionType, BackendType, Idempotency,
    PermissionDecision, Reversibility, ToolSpec,
)
from agent_runtime.tools.registry import PermissionEngine, ToolRegistry


def make_spec(**kwargs):
    defaults = dict(
        id="write_file",
        description="写文件",
        action_type=ActionType.WRITE,
        reversible=Reversibility.DECLARED_REVERSIBLE,
        idempotent=Idempotency.IDEMPOTENT,
        supported_backends=frozenset({BackendType.LOCAL_FS}),
        max_execution_time_s=30,
        compensation_tool_id=None,
    )
    defaults.update(kwargs)
    return ToolSpec(**defaults)


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


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        spec = make_spec()
        reg.register(spec)
        assert reg.get("write_file") == spec

    def test_register_handler_and_execute(self):
        reg = ToolRegistry()
        reg.register(make_spec())
        reg.register_handler("write_file", lambda params: {"ok": True})
        result = reg.execute("write_file", {})
        assert result == {"ok": True}

    def test_execute_passes_workspace_path(self):
        reg = ToolRegistry()
        reg.register(make_spec())
        received = {}

        def handler(params, workspace_path=None):
            received["ws"] = workspace_path
            return {}

        reg.register_handler("write_file", handler)
        reg.execute("write_file", {}, workspace_path="/tmp/ws")
        assert received["ws"] == "/tmp/ws"

    def test_execute_no_handler_raises(self):
        reg = ToolRegistry()
        reg.register(make_spec())
        with pytest.raises(KeyError, match="No handler registered"):
            reg.execute("write_file", {})

    def test_register_handler_unknown_tool_raises(self):
        reg = ToolRegistry()
        with pytest.raises(KeyError):
            reg.register_handler("nonexistent", lambda p: p)

    def test_register_missing_timeout_raises(self):
        reg = ToolRegistry()
        with pytest.raises(ValueError, match="max_execution_time_s"):
            reg.register(make_spec(max_execution_time_s=None))

    def test_register_invalid_backend_type_raises(self):
        reg = ToolRegistry()
        with pytest.raises(TypeError, match="BackendType instances"):
            reg.register(make_spec(supported_backends=frozenset({"local_fs"})))


class TestPermissionEngine:
    def _make_engine(self, backend=BackendType.LOCAL_FS):
        reg = ToolRegistry()
        reg.register(make_spec())
        return PermissionEngine(reg, backend), reg

    def test_reversible_action_allowed(self):
        engine, _ = self._make_engine()
        decision = engine.check(make_action())
        assert decision.allowed is True
        assert decision.requires_hitl is False

    def test_unknown_tool_hard_reject(self):
        engine, _ = self._make_engine()
        decision = engine.check(make_action(tool_id="nonexistent"))
        assert decision.allowed is False
        assert decision.hard_reject is True

    def test_unsupported_backend_hard_reject(self):
        engine, _ = self._make_engine(backend=BackendType.GCS)
        decision = engine.check(make_action())
        assert decision.allowed is False
        assert decision.hard_reject is True

    def test_irreversible_requires_hitl(self):
        reg = ToolRegistry()
        reg.register(make_spec(id="del", reversible=Reversibility.IRREVERSIBLE))
        engine = PermissionEngine(reg, BackendType.LOCAL_FS)
        decision = engine.check(make_action(tool_id="del"))
        assert decision.allowed is False
        assert decision.requires_hitl is True
