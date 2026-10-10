# agent-runtime

这是一个 Agent 运行时框架的骨架实现，用于探索自主任务执行的核心抽象和契约设计。

## 项目说明

本项目是一个研究型的 Agent Runtime 原型，实现了以下核心组件：

- **Contracts（contracts.py）**：系统类型定义和契约，包括 Goal、Task、Action、Observe 等基础抽象，以及状态机、权限模型和 trace 系统的类型定义
- **Kernel（kernel/）**：运行时核心
  - `runtime.py`：任务执行引擎
  - `scheduler.py`：任务调度器，管理任务队列和优先级
  - `state_machine.py`：任务状态机，管理状态转换和不变式验证
- **Planner（planner/）**：基于 SiliconFlow LLM 的规划器，将 Goal 转换为 ActionRequest
- **Tools（tools/）**：工具注册表和内置工具
- **Trace（trace/）**：执行追踪和可观测性
- **Storage（storage/）**：存储抽象和本地文件系统实现
- **Context（context/）**：上下文存储，记录会话和项目级别的信息

详细的设计评审和已知问题请参见 [REVIEW.md](./REVIEW.md)。

## 安装

```bash
pip install -e ".[dev]"
```

依赖要求：
- Python >= 3.9
- httpx >= 0.27

## 运行测试

```bash
# 运行所有单元测试（不需要网络）
pytest tests/ -v

# 运行端到端测试（需要 SILICONFLOW_API_KEY）
SILICONFLOW_API_KEY=sk-xxx pytest tests/e2e_test.py -v -s
```

## 环境变量

- `SILICONFLOW_API_KEY`：SiliconFlow API 密钥（仅 e2e 测试需要）

## 项目状态

⚠️ **本项目已归档。** 

这是一个概念验证和设计探索项目，用于建立 Agent Runtime 的基础契约和抽象。后续工作已在以下仓库继续：

- **后续运行时实现**：https://github.com/wangwu-30/tobe
- **早期内核研究**：https://github.com/wangwu-30/AOS

## 许可证

MIT License - 详见 [LICENSE](./LICENSE)

---

# agent-runtime

A skeleton implementation of an Agent Runtime framework exploring core abstractions and contracts for autonomous task execution.

## Overview

This is a research prototype implementing:

- **Contracts**: Type definitions for Goal, Task, Action, Observe, state machine, permission model, and trace system
- **Kernel**: Task execution engine, scheduler, and state machine
- **Planner**: LLM-based planner using SiliconFlow API to convert Goals into ActionRequests
- **Tools**: Tool registry and built-in tools
- **Trace**: Execution tracing and observability
- **Storage**: Storage abstraction with local filesystem backend
- **Context**: Session and project-level context storage

For design review and known issues, see [REVIEW.md](./REVIEW.md).

## Installation

```bash
pip install -e ".[dev]"
```

## Running Tests

```bash
# Unit tests (no network required)
pytest tests/ -v

# End-to-end tests (requires SILICONFLOW_API_KEY)
SILICONFLOW_API_KEY=sk-xxx pytest tests/e2e_test.py -v -s
```

## Environment Variables

- `SILICONFLOW_API_KEY`: SiliconFlow API key (required for e2e tests only)

## Project Status

⚠️ **This project is archived.**

This was a proof-of-concept for establishing Agent Runtime contracts and abstractions. Subsequent work continues at:

- **Runtime implementation**: https://github.com/wangwu-30/tobe
- **Early kernel research**: https://github.com/wangwu-30/AOS

## License

MIT License - see [LICENSE](./LICENSE)
