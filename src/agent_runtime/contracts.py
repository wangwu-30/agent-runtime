"""
Agent Runtime — Core Contracts

所有组件共享的类型定义。这是系统的底层契约，不依赖任何其他模块。

设计来源：DESIGN.md + ADR-001 到 ADR-016
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


# ---------------------------------------------------------------------------
# Identity types
# ---------------------------------------------------------------------------

SessionId = str   # 会话 ID，message 到达时分配，不依赖 task
GoalId = str      # GOAL 节点 ID，贯穿整个 DAG
TaskId = str      # task 执行单元 ID，clarifier 完成后分配
TraceId = str     # trace 根 ID，和顶层 GOAL 绑定
SpanId = str      # span ID，每个 task 一个 span
WorkerId = str    # worker ID
ProjectId = str   # project 隔离边界


# ---------------------------------------------------------------------------
# Core primitives: GOAL / ACTION / OBSERVE
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Goal:
    """GOAL 节点。定义权属于用户，系统只能澄清/分解，不能替换。(SA1)"""
    id: GoalId
    content: str                        # 自然语言描述
    success_criteria: Optional[str]     # 可验证的成功条件，None 表示需要 clarifier 澄清
    parent_goal_id: Optional[GoalId]    # 根节点为 None
    root_goal_id: GoalId                # 追溯到根 GOAL（OB1 可追溯性）
    session_id: SessionId
    interpretation_version: int = 0     # ADR-003: GOAL 版本锁，澄清时递增
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ResourceRef:
    """资源引用。用于锁顺序、权限检查、循环检测。"""
    kind: str           # "file", "api_endpoint", "database_table", "external_service"
    path: str           # 资源路径（文件路径、URL、表名等）
    project_id: ProjectId

    def lock_key(self) -> str:
        """全局字典序锁顺序的 key。(ADR-011)
        # TODO(v2): 此方法用于 ADR-011 的全局字典序锁获取
        # 当前未被调用，锁管理器（LockManager）在 v2 实现
        """
        return f"{self.project_id}:{self.kind}:{self.path}"


@dataclass(frozen=True)
class ActionRequest:
    """ACTION 请求。executor 产生，permission_engine 检查，action_runner 执行。

    注意：action_type / reversible / idempotent 从 ToolSpec（registry）读取，
    不在 ActionRequest 中重复存储。(问题7: 单一权威来源)
    """
    id: str
    tool_id: str
    params: Dict[str, Any]
    goal_ref: GoalId
    span_id: SpanId
    resources: Sequence[ResourceRef]    # 已知的资源访问集合（HC4 最小副作用范围）
    explore_intent: bool = False        # ADR-004: 是否为探索性 ACTION
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ObserveEvent:
    """OBSERVE 事件。来自系统边界之外，不由系统内部生成。(SA2)"""
    action_id: str
    span_id: SpanId
    raw_output: Any                     # ACTION 执行的原始输出
    resource_versions: Dict[str, str]   # ADR-013: 资源路径 → 版本/commit hash
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    backend: "BackendType" = field(default_factory=lambda: BackendType.LOCAL_FS)  # 问题4: 类型为 BackendType


@dataclass(frozen=True)
class EvalResult:
    """evaluator 的判断结果。"""
    goal_satisfied: bool
    failure_type: Optional["FailureType"]   # O2a / O2b / O2c，goal_satisfied=False 时必填
    confidence: float                        # 0.0 - 1.0
    rationale: str
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        # 问题9: 不变式编码——失败时 failure_type 必须非 None
        if not self.goal_satisfied and self.failure_type is None:
            raise ValueError("EvalResult: failure_type is required when goal_satisfied=False")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskState(enum.Enum):
    """task 状态机。合法转换见 DESIGN.md §7.1。(ADR-013)"""
    PENDING = "pending"
    RUNNING = "running"
    VALIDATING = "validating"
    DONE = "done"
    # 等待类
    SUSPENDED = "suspended"             # 系统异步等待，不持锁 (ADR-013)
    HITL_WAITING = "hitl_waiting"       # 人类审批，持锁，scheduler 续租 (ADR-014)
    CLARIFYING = "clarifying"           # O2a，GOAL 歧义，不持锁
    REPLANNING = "replanning"
    # 终态
    RETRYING = "retrying"
    FAILED = "failed"
    DEAD = "dead"
    CANCELLED = "cancelled"
    REPLAN_REQUIRED = "replan_required" # ADR-014: scheduler 续租失败后的降级状态


# 合法状态转换表（用于状态机校验）
LEGAL_TRANSITIONS: Dict[TaskState, frozenset] = {
    TaskState.PENDING:          frozenset({TaskState.RUNNING}),
    TaskState.RUNNING:          frozenset({
                                    TaskState.VALIDATING,
                                    TaskState.SUSPENDED,
                                    TaskState.HITL_WAITING,
                                    TaskState.REPLANNING,
                                    TaskState.FAILED,
                                }),
    TaskState.VALIDATING:       frozenset({
                                    TaskState.DONE,
                                    TaskState.REPLANNING,       # O2b
                                    TaskState.RETRYING,         # O2c
                                    TaskState.CLARIFYING,       # O2a
                                    TaskState.FAILED,           # validation failure
                                }),
    TaskState.SUSPENDED:        frozenset({
                                    TaskState.PENDING,          # 恢复条件满足，重新入队
                                    TaskState.HITL_WAITING,     # 需要人类决策（必须先重新获取锁）
                                    TaskState.FAILED,           # 超时
                                }),
    TaskState.HITL_WAITING:     frozenset({
                                    TaskState.RUNNING,          # 审批通过
                                    TaskState.CANCELLED,
                                    TaskState.REPLAN_REQUIRED,  # 续租失败 fail-closed (ADR-014)
                                }),
    TaskState.CLARIFYING:       frozenset({
                                    TaskState.RUNNING,          # 澄清完成，重新执行
                                    TaskState.CANCELLED,
                                }),
    TaskState.REPLANNING:       frozenset({
                                    TaskState.RUNNING,
                                    TaskState.FAILED,
                                }),
    TaskState.RETRYING:         frozenset({
                                    TaskState.RUNNING,
                                    TaskState.DEAD,
                                    TaskState.HITL_WAITING,     # 不可逆 ACTION 失败后必须重新授权
                                }),
    TaskState.REPLAN_REQUIRED:  frozenset({
                                    TaskState.REPLANNING,       # 用户/系统触发重新规划
                                    TaskState.CANCELLED,
                                }),
    # 终态：无合法出口
    TaskState.DONE:             frozenset(),
    TaskState.FAILED:           frozenset({TaskState.RETRYING}),
    TaskState.DEAD:             frozenset(),
    TaskState.CANCELLED:        frozenset(),
}


class ActionType(enum.Enum):
    READ = "R"      # 只读
    WRITE = "W"     # 写入
    EXECUTE = "E"   # 执行外部效果（发送消息、调用 API 等）


class Reversibility(enum.Enum):
    """工具可逆性声明。(ADR-008)"""
    DECLARED_REVERSIBLE = "declared_reversible"     # 声明可逆，未经验证
    VERIFIED_REVERSIBLE = "verified_reversible"     # 通过级别2自动验证
    IRREVERSIBLE = "irreversible"                   # 不可逆，执行前强制 HITL
    UNKNOWN = "unknown"                             # 未声明，按不可逆处理


class Idempotency(enum.Enum):
    """工具幂等性声明。(ADR-016)"""
    IDEMPOTENT = "idempotent"       # 重复执行结果等价，可安全重试
    NON_IDEMPOTENT = "non_idempotent"  # 重复执行可能产生不同结果
    UNKNOWN = "unknown"             # 未声明，按非幂等处理


class BackendType(enum.Enum):
    """存储 backend 类型。(ADR-002, ADR-016)"""
    LOCAL_FS = "local_fs"   # 本地文件系统，满足 trace 强一致性要求
    GCS = "gcs"             # Google Cloud Storage，满足 trace 强一致性要求
    S3 = "s3"               # 禁止用于 trace 存储（eventual consistency）


TRACE_ALLOWED_BACKENDS = frozenset({BackendType.LOCAL_FS, BackendType.GCS})


class FailureType(enum.Enum):
    """OBSERVE 不满足 GOAL 时的失败分类。混淆这三类是系统无法收敛的根本原因。"""
    O2A = "o2a"     # GOAL 定义有歧义 → clarifier
    O2B = "o2b"     # 分解策略错误 → replan
    O2C = "o2c"     # ACTION 执行失败 → retry


class RiskLevel(enum.Enum):
    """ACTION 风险级别。(ADR-007: 风险单位是 ACTION，不是 task)"""
    LOW = "low"         # 可逆，完全确定 → Auto judge
    MEDIUM = "medium"   # 可逆但参数不确定 → Auto + 阈值告警
    HIGH = "high"       # 不可逆或静态不可分析 → 强制 HITL


class ScopeLevel(enum.Enum):
    """context 记录的 scope 级别。"""
    EPHEMERAL = "ephemeral"     # task 结束即过期
    TASK = "task"               # task 完成后 N 天
    PROJECT = "project"         # project 结束后
    PERMANENT = "permanent"     # 需要显式 review


class SpanStatus(enum.Enum):
    """Span 状态。(问题9: str → enum)"""
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class AttemptStatus(enum.Enum):
    """ActionAttempt 状态。(问题9: str → enum, ADR-016)"""
    PENDING = "pending"
    COMMITTED = "committed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """task 执行单元。"""
    id: TaskId
    goal: Goal
    state: TaskState
    session_id: SessionId
    trace_id: TraceId
    span_id: SpanId
    parent_task_id: Optional[TaskId]    # 根 task 为 None
    project_id: ProjectId
    # 调度
    priority: float = 0.0               # ADR-010: 优先级队列排序
    worker_id: Optional[WorkerId] = None
    # 状态相关
    replan_count: int = 0
    retry_count: int = 0
    max_retry: int = 3                  # 最大重试次数（#5）
    # SUSPENDED 相关 (ADR-013)
    resume_condition: Optional["ResumeCondition"] = None
    resource_versions_at_suspend: Optional[Dict[str, str]] = None  # world-state generation check
    # 时间
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ResumeCondition:
    """SUSPENDED task 的恢复条件。(ADR-013)"""
    condition_type: "ResumeConditionType"
    condition_detail: Dict[str, Any] = field(default_factory=dict)
    timeout_at: Optional[datetime] = None   # None 表示无超时（需要显式声明原因）
    timeout_reason: Optional[str] = None    # timeout_at=None 时必须填写原因

    def __post_init__(self) -> None:
        # 问题9: 不变式编码——无超时时必须声明原因
        if self.timeout_at is None and self.timeout_reason is None:
            raise ValueError(
                "ResumeCondition: timeout_reason is required when timeout_at=None. "
                "Explicitly justify why this condition has no timeout."
            )


class ResumeConditionType(enum.Enum):
    SUBTASK_DONE = "subtask_done"
    EXTERNAL_EVENT = "external_event"
    TIMER = "timer"
    AMBIGUITY_RESOLVED = "ambiguity_resolved"       # ADR-003 ambiguity_signal 解决
    CROSS_PROJECT_AUTHORIZED = "cross_project_authorized"  # ADR-004


# ---------------------------------------------------------------------------
# Signals（上报机制，类似 ambiguity_signal 模式）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AmbiguitySignal:
    """并行子树发现 GOAL 歧义时上报给父节点。(ADR-003)"""
    source_span_id: SpanId
    parent_goal_id: GoalId
    ambiguity_description: str
    affected_goal_version: int
    reported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CrossProjectExploreSignal:
    """探索阶段发现需要跨 project 时上报给父节点。(ADR-004)"""
    source_span_id: SpanId
    reason: str
    target_scope: str
    goal_ref: GoalId
    current_context_summary: str
    reported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Permission
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PermissionDecision:
    """权限检查结果。"""
    allowed: bool
    reason: str
    requires_hitl: bool = False     # 需要 HITL 审批
    hard_reject: bool = False       # 直接拒绝（backend 不匹配等）


@dataclass(frozen=True)
class ToolSpec:
    """工具注册规范。(ADR-008, ADR-016)"""
    id: str
    description: str
    action_type: ActionType
    reversible: Reversibility
    idempotent: Idempotency
    supported_backends: frozenset[BackendType]  # 支持的 backend 类型，(tool, backend) 决定可逆性
    max_execution_time_s: Optional[int]  # ADR-006: 必须声明，None 需要显式授权
    compensation_tool_id: Optional[str]  # 补偿 ACTION 的 tool_id
    permission_schema: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Span / Trace
# ---------------------------------------------------------------------------

@dataclass
class Span:
    """trace 的基本单元，对应一个 task 的执行记录。(DESIGN.md §九)"""
    id: SpanId
    trace_id: TraceId
    parent_span_id: Optional[SpanId]
    task_id: TaskId
    goal: Goal
    worker_id: Optional[WorkerId]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: Optional[datetime] = None
    status: SpanStatus = SpanStatus.RUNNING   # 问题9: str → enum
    superseded_by: Optional[SpanId] = None    # replan 时被哪个 span 替代（HC6 不删历史）
    context_refs: List[str] = field(default_factory=list)  # 引用的 context 记录 ID


@dataclass(frozen=True)
class ActionAttempt:
    """ACTION 执行尝试记录。(ADR-016: T2-T4 崩溃窗口保护)

    执行前写入 PENDING，执行后写入 COMMITTED。
    恢复时：有 PENDING 无 COMMITTED → 崩溃窗口，按幂等性决策。
    status 只存在于 trace 文件中（action_attempts.jsonl），不在此对象上。(#2)
    """
    id: str
    span_id: SpanId
    action_request: ActionRequest
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Storage backend contract
# ---------------------------------------------------------------------------

class StorageError(Exception):
    pass


class BackendNotSupportedError(StorageError):
    """当前 backend 不在工具的 supported_backends 中。(ADR-008)"""
    pass


class TraceBackendError(StorageError):
    """trace 存储 backend 不满足强一致性要求。(ADR-016)"""
    pass
