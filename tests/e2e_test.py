"""
End-to-end test: goal → SiliconFlow LLM planner → write_file → DONE

需要环境变量：SILICONFLOW_API_KEY=sk-xxx
运行方式：
    SILICONFLOW_API_KEY=sk-xxx pytest tests/e2e_test.py -v -s
"""

import os
import tempfile
from pathlib import Path

import pytest

from agent_runtime.kernel.runtime import RuntimeKernel
from agent_runtime.planner.llm import SiliconFlowPlanner
from agent_runtime.runner.local import LocalRunner, WorktreeManager
from agent_runtime.storage.local import LocalFSBackend
from agent_runtime.tools.builtin import register_builtin_tools
from agent_runtime.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(
    not os.environ.get("SILICONFLOW_API_KEY"),
    reason="SILICONFLOW_API_KEY not set",
)


@pytest.fixture
def tmp_root(tmp_path):
    return tmp_path


def make_kernel(tmp_root: Path) -> RuntimeKernel:
    storage = LocalFSBackend(root=str(tmp_root / ".trace"))
    registry = ToolRegistry()
    register_builtin_tools(registry)
    wm = WorktreeManager(str(tmp_root / "repo"), use_git=False)
    runner = LocalRunner(wm)
    return RuntimeKernel(
        storage=storage,
        registry=registry,
        runner=runner,
        project_id="e2e-test",
    )


@pytest.mark.asyncio
async def test_e2e_write_file(tmp_root):
    kernel = make_kernel(tmp_root)
    api_key = os.environ["SILICONFLOW_API_KEY"]
    planner = SiliconFlowPlanner(api_key=api_key)
    kernel.set_planner(planner)

    result = await kernel.handle_message(
        goal_content="请在 workspace 中创建一个名为 hello.txt 的文件，内容为 'Hello, Agent Runtime!'",
        session_key="e2e-session-1",
        success_criteria="hello.txt 文件存在且内容正确",
    )

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result"] is not None
    written_path = result["result"].get("written", "")
    assert written_path.endswith("hello.txt"), f"Expected hello.txt, got: {written_path}"

    # 验证文件实际写入
    assert Path(written_path).exists(), f"File not found: {written_path}"
    content = Path(written_path).read_text(encoding="utf-8")
    assert "Hello" in content, f"Unexpected content: {content!r}"


@pytest.mark.asyncio
async def test_e2e_trace_files_created(tmp_root):
    """验证 trace 目录下有对应的 JSON 文件。"""
    kernel = make_kernel(tmp_root)
    api_key = os.environ["SILICONFLOW_API_KEY"]
    planner = SiliconFlowPlanner(api_key=api_key)
    kernel.set_planner(planner)

    result = await kernel.handle_message(
        goal_content="创建文件 trace_check.txt，内容为 'trace ok'",
        session_key="e2e-session-2",
        success_criteria="文件创建成功",
    )

    assert result["status"] == "done"

    trace_root = tmp_root / ".trace"
    trace_files = list(trace_root.rglob("*.json")) + list(trace_root.rglob("*.jsonl"))
    assert len(trace_files) > 0, "No trace files found"
