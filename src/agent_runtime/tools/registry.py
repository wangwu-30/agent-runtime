"""
Tools — ToolRegistry + PermissionEngine

工具注册、权限检查、可逆性验证。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from agent_runtime.contracts import (
    ActionRequest, ActionType, BackendType, BackendNotSupportedError,
    PermissionDecision, Reversibility, RiskLevel, TaskId, ToolSpec,
)

logger = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册中心。注册时验证 ToolSpec 完整性。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._handlers: Dict[str, Callable] = {}  # P0-1: tool_id → handler fn

    def register(self, spec: ToolSpec) -> None:
        if spec.max_execution_time_s is None and spec.id not in _EXPLICIT_NO_TIMEOUT_WHITELIST:
            raise ValueError(
                f"Tool {spec.id}: max_execution_time_s must be declared. "
                f"Set to None only with explicit justification. (ADR-006)"
            )
        # #1: 注册时校验 supported_backends 元素类型
        for b in spec.supported_backends:
            if not isinstance(b, BackendType):
                raise TypeError(
                    f"Tool {spec.id}: supported_backends must contain BackendType instances, got {type(b)}"
                )
        self._tools[spec.id] = spec
        logger.info("Registered tool: %s (reversible=%s, idempotent=%s)",
                    spec.id, spec.reversible.value, spec.idempotent.value)

    def register_handler(self, tool_id: str, fn: Callable) -> None:
        """注册工具的执行逻辑。ToolSpec 是元数据，handler 是执行逻辑。(P0-1)"""
        if tool_id not in self._tools:
            raise KeyError(f"Tool {tool_id} not registered. Register ToolSpec first.")
        self._handlers[tool_id] = fn

    def execute(self, tool_id: str, params: dict, workspace_path: str = None) -> Any:
        """执行已注册的工具 handler。(P0-1, P0-3)
        若 handler 接受 workspace_path 参数，则传入；否则忽略。
        """
        import inspect
        fn = self._handlers.get(tool_id)
        if fn is None:
            raise KeyError(f"No handler registered for tool: {tool_id}. Call register_handler() first.")
        sig = inspect.signature(fn)
        if 'workspace_path' in sig.parameters:
            return fn(params, workspace_path=workspace_path)
        return fn(params)

    def get(self, tool_id: str) -> Optional[ToolSpec]:
        return self._tools.get(tool_id)

    def require(self, tool_id: str) -> ToolSpec:
        spec = self._tools.get(tool_id)
        if spec is None:
            raise KeyError(f"Tool not registered: {tool_id}")
        return spec


# 需要显式声明"无超时"的工具白名单（必须有业务理由）
_EXPLICIT_NO_TIMEOUT_WHITELIST: frozenset = frozenset()


class PermissionEngine:
    """
    ACTION 执行前的权限检查。
    在同一决策点完成：权限 × 可逆性 × 可分析性 × backend 一致性。(DESIGN.md §7.6)

    权威来源：action_type / reversible / idempotent 只从 ToolSpec（registry）读取，
    不从 ActionRequest 读取。(问题7)
    """

    def __init__(self, registry: ToolRegistry, current_backend: BackendType) -> None:
        self._registry = registry
        self._backend = current_backend

    def check(self, action: ActionRequest) -> PermissionDecision:
        """
        返回 PermissionDecision。
        hard_reject=True 表示直接拒绝，不进 HITL。
        requires_hitl=True 表示需要人类审批。

        所有行为相关字段（action_type, reversible, idempotent）只从 spec 读取。(问题7)
        """
        spec = self._registry.get(action.tool_id)
        if spec is None:
            return PermissionDecision(
                allowed=False,
                reason=f"Tool {action.tool_id} not registered",
                hard_reject=True,
            )

        # 1. Backend 一致性检查（ADR-008）
        # #7: 统一返回 PermissionDecision，不 raise（PermissionEngine 接口一致性）
        if spec.supported_backends and self._backend not in spec.supported_backends:
            return PermissionDecision(
                allowed=False,
                reason=(
                    f"Tool {spec.id} does not support backend {self._backend.value}. "
                    f"Supported: {[b.value for b in spec.supported_backends]}"
                ),
                hard_reject=True,
            )

        # 2. 不可逆 ACTION 强制 HITL（HC3）——从 spec 读取（问题7）
        if spec.reversible in {Reversibility.IRREVERSIBLE, Reversibility.UNKNOWN}:
            return PermissionDecision(
                allowed=False,
                reason=f"Tool {spec.id} is irreversible, requires explicit HITL authorization",
                requires_hitl=True,
            )

        # 3. 静态不可分析的 ACTION → HITL（DESIGN.md §7.6）——从 spec 读取（问题7）
        if spec.action_type == ActionType.EXECUTE and not action.explore_intent:
            risk = _compute_risk(spec)
            if risk == RiskLevel.HIGH:
                return PermissionDecision(
                    allowed=False,
                    reason=f"Tool {spec.id} is high-risk, requires HITL authorization",
                    requires_hitl=True,
                )

        return PermissionDecision(allowed=True, reason="ok")

    def check_compensation(self, original_action: ActionRequest, comp_tool_id: str) -> PermissionDecision:
        """
        补偿 ACTION 必须在与原始 ACTION 相同的 backend 下执行。(ADR-008)
        """
        spec = self._registry.get(comp_tool_id)
        if spec is None:
            return PermissionDecision(
                allowed=False,
                reason=f"Compensation tool {comp_tool_id} not registered",
                hard_reject=True,
            )
        if self._backend not in spec.supported_backends:
            return PermissionDecision(
                allowed=False,
                reason=(
                    f"Compensation tool {comp_tool_id} does not support "
                    f"current backend {self._backend.value}. "
                    f"Cannot execute compensation in a different backend than original action."
                ),
                requires_hitl=True,
            )
        return PermissionDecision(allowed=True, reason="ok")


def _compute_risk(spec: ToolSpec) -> RiskLevel:
    """ACTION 风险 = f(可逆性, 不确定性)。(ADR-007) 只从 spec 读取字段。"""
    if spec.reversible in {Reversibility.IRREVERSIBLE, Reversibility.UNKNOWN}:
        return RiskLevel.HIGH
    if spec.action_type == ActionType.EXECUTE:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW
