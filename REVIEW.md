# Code Review — agent-runtime 框架骨架

审查时间：2026-04-16  
审查范围：contracts.py / kernel / runner / trace / storage / tools / context

---

## 一、contracts.py 类型设计问题

### 1. `ToolSpec.supported_backends` 缺少泛型参数

```python
supported_backends: frozenset  # 应为 frozenset[BackendType]
```

`PermissionEngine.check()` 做 `self._backend not in spec.supported_backends`，若有人传 `frozenset({"local_fs"})` 字符串，比较永远不匹配且静默失败。应改为 `frozenset[BackendType]`，并在 `ToolRegistry.register()` 加类型校验。

### 2. `ActionAttempt` 是 `frozen=True`，但语义上需要状态变更

`ActionAttempt.status` 永远是 `PENDING`，`commit_action_attempt` 只往 JSONL 追加新记录，不修改对象。调用方持有的对象和 trace 里的实际状态不一致。  
**选择一：** 去掉 `frozen=True`，让 `commit_action_attempt` 同时更新对象 status。  
**选择二：** 从 `ActionAttempt` 删掉 `status` 字段，status 只存在于 trace 文件里。

### 3. 全库使用 naive datetime，Python 3.12 已 deprecate `datetime.utcnow()`

`Goal.created_at`、`Span.started_at`、`ObserveEvent.observed_at` 等全部是无时区的 naive datetime，序列化后 ISO 字符串没有 `+00:00`，反序列化时丢失时区信息。统一改为 `datetime.now(timezone.utc)`。

### 4. `Task` 进入 `SUSPENDED` 时 `resume_condition` 可以是 `None`

状态机允许 `RUNNING → SUSPENDED`，但 `Task.resume_condition` 是 `Optional[ResumeCondition]`，没有任何地方强制要求非 None。这违反了 SUSPENDED 的 quiescent boundary 不变式。应在 `TaskStateMachine.transition()` 里对 `→ SUSPENDED` 的转换做前置检查。

### 5. `Task` 有 `retry_count` 但没有 `max_retry`，上限在哪里定义不明确

`FAILED → RETRYING → DEAD` 路径存在，但何时从 `RETRYING` 转到 `DEAD` 取决于 retry 上限，contracts 里没有定义。

---

## 二、模块接口不一致问题

### 6. `_write_observe_summary_with_session` 用 `inspect.signature` 做运行时鸭子类型检测

```python
sig = inspect.signature(self._context.write_observe_summary)
if "session_id" in sig.parameters:  # 永远为 False
```

`ContextStore.write_observe_summary` 根本没有 `session_id` 参数，这段代码永远走 `else` 分支，`inspect` 调用是死代码。应直接在 `write_observe_summary` 加 `session_id` 参数，或把 session active context 更新逻辑移入 `ContextStore`。

### 7. `PermissionEngine.check()` backend 不匹配时 `raise`，其他错误路径 `return`

调用方 `runtime.py` 只处理 `decision.allowed == False`，没有 `try/except BackendNotSupportedError`。backend 不匹配会穿透到 `_execute_task` 的 `except Exception`，走 `discard` 路径，但不会把具体原因写入 trace。接口应统一：要么全部返回 `PermissionDecision`，要么全部抛异常。

### 8. `TraceStore` 的 `status` 参数类型是 `Any`，兼容层是技术债

`close_span(status)` 接受 `SpanStatus` 或字符串，用 `_span_status_value()` 做兼容。`SpanStatus` 已在 contracts 里，应直接用 `SpanStatus`，删掉兼容代码和 `try/except ImportError` 块。

### 9. `Scheduler._task_states` 和 `Task.state` 是两套独立状态，没有 source of truth

`dequeue()` 直接赋值 `self._task_states[task_id] = TaskState.RUNNING`，绕过了 `TaskStateMachine`。`runtime.py` 里 `task.state = self._sm.transition(...)` 改的是 `Task` 对象。两套状态可以漂移，v1 单 task 不出问题，多 task 或 worker 时会出问题。

### 10. `prepare_workspace` 失败时 `begin_span` 可能还未执行，但 `close_span` 仍会被调用

`prepare_workspace` 在 `begin_span` 之前，若 git 操作失败，`except` 块里调 `close_span` 会往不存在的路径追加，`LocalFSBackend.append` 会创建孤立文件。应调整执行顺序：先 `begin_span`，再 `prepare_workspace`。

---

## 三、第一个垂直切片（单叶子节点，真实文件写入）需要做什么

### P0 — 阻塞跑通

**1. `ToolRegistry` 加 handler 注册机制**

`ToolSpec` 是纯元数据，没有地方存工具的执行逻辑。这是当前最大的结构性缺口。最简方案：

```python
# ToolRegistry 加一个方法
def register_handler(self, tool_id: str, fn: Callable[[dict], Any]) -> None:
    self._handlers[tool_id] = fn

def execute(self, tool_id: str, params: dict) -> Any:
    fn = self._handlers.get(tool_id)
    if fn is None:
        raise KeyError(f"No handler for tool: {tool_id}")
    return fn(params)
```

**2. 实现 `_run_action()`**

```python
def _run_action(self, action: ActionRequest, workspace) -> Any:
    return self._registry.execute(action.tool_id, action.params)
```

**3. 实现 `_plan_leaf_action()`（v1 不需要 LLM）**

从 registry 取第一个工具，用 goal content 构造 params：

```python
def _plan_leaf_action(self, goal: Goal, workspace) -> ActionRequest:
    # v1: 调用方在构造 RuntimeKernel 前注册好工具和默认 planner
    raise NotImplementedError(...)
    # 实际上需要调用方通过 set_planner() 注入，或硬编码 write_file 工具
```

最简方案：加 `set_planner(fn)` 注入点，让调用方决定如何从 goal 映射到 ActionRequest。

**4. 注册 `write_file` 工具**

```python
registry.register(ToolSpec(
    id="write_file",
    description="写入文件内容",
    action_type=ActionType.WRITE,
    reversible=Reversibility.DECLARED_REVERSIBLE,
    idempotent=Idempotency.IDEMPOTENT,
    supported_backends=frozenset({BackendType.LOCAL_FS}),
    max_execution_time_s=30,
    compensation_tool_id=None,
))
registry.register_handler("write_file", lambda p: Path(p["path"]).write_text(p["content"]))
```

### P1 — 影响本地开发体验

**5. `LocalRunner` 支持非 git 模式**

v1 切片不需要 git worktree，加 `use_git: bool = True` 参数，`False` 时直接用 `tempfile.mkdtemp()`。

**6. 删除 `inspect.signature` 死代码，修复 session context 写入**

### P2 — 不阻塞但应在切片完成前修复

**7. 统一 `PermissionEngine` 异常/返回值**  
**8. 端到端集成测试**：验证 `.trace/` 和 `.context/` 文件确实写入，内容正确。

---

## 四、设计上有问题但代码里没体现的地方

### 11. `CLARIFYING → RUNNING` 需要重新获锁，代码里没有体现

`VALIDATING → CLARIFYING` 时锁已释放（`holds_locks()` 里 `VALIDATING` 不持锁），`CLARIFYING → RUNNING` 时需要重新获取 worktree 锁，但 `runtime.py` 里没有对应逻辑，也没有 TODO。

### 12. `ObserveEvent.resource_versions` 永远是空字典，SUSPENDED world-state check 形同虚设

`Task.resource_versions_at_suspend` 存 suspend 时的版本，resume 时要和当前版本比较。v1 里 `resource_versions={}` 导致这个检查永远通过。不是 v1 的问题，但应加 TODO，否则后续实现者不知道需要填充。

### 13. `ResourceRef.lock_key()` 存在但从未被调用

ADR-011 的锁顺序设计在 contracts 里有 `lock_key()` 方法，但 `PermissionEngine`、`RuntimeKernel`、`LocalRunner` 里都没有实际的锁获取逻辑，也没有 TODO 标注。这是核心安全约束，不应该无声无息地缺失。

### 14. `LEGAL_TRANSITIONS` 里 `RUNNING` 不能直接转到 `CLARIFYING`

`CLARIFYING` 只能从 `VALIDATING` 进入（O2a 路径），但 `is_waiting()` 把 `CLARIFYING` 列为不发 heartbeat 的状态，注释里说"不占用 worker"。这是对的，但容易让实现者误以为可以从 `RUNNING` 直接进入 `CLARIFYING`，应在注释里明确说明入口只有 `VALIDATING → CLARIFYING`。

---

## TODO: task result 契约修复

**问题**：`runtime.py:227` 直接把 `raw_result`（工具原始输出，含临时路径）作为 task result 返回。
git 模式下 `merge_if_valid` 已把产物 merge 进主 repo，但返回的路径仍是已删除的 workspace 路径。
no-git 模式下 workspace 被 `rmtree`，路径完全失效。

**根因**：task result 和 tool result 没有分层。raw_result 是执行细节，属于 trace；task result 应描述 durable postcondition。

**设计结论**（来自代码审查 + Codex consult）：
- runtime 是执行引擎，持久化机制是 git merge（no-git 模式无持久化，by design）
- task result 需要三层分离：tool result（trace 用）→ observation normalization → task result（consumer 用）
- no-git 模式不应返回文件路径作为 result；git 模式应返回 repo 内的逻辑路径

**待决策**（需要 force-thinker FORMALIZATION）：
- task result 的正确数据结构（`outputs` 列表 vs 其他）
- no-git 模式的 result 语义（null locator / 纯内容摘要 / 明确不支持持久产物）
- 是否引入 `TaskOutput` 类型到 contracts.py

**涉及文件**：`contracts.py`、`kernel/runtime.py`、`tools/builtin.py`、`tests/e2e_test.py`
