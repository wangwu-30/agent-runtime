"""内置工具：write_file。用于 v1 垂直切片验证。"""
from pathlib import Path

from agent_runtime.contracts import ActionType, BackendType, Idempotency, Reversibility, ToolSpec
from agent_runtime.tools.registry import ToolRegistry


def register_builtin_tools(registry: ToolRegistry) -> None:
    """注册所有内置工具。"""
    _register_write_file(registry)


def _register_write_file(registry: ToolRegistry) -> None:
    spec = ToolSpec(
        id="write_file",
        description="写入文件内容到 workspace",
        action_type=ActionType.WRITE,
        reversible=Reversibility.DECLARED_REVERSIBLE,
        idempotent=Idempotency.IDEMPOTENT,
        supported_backends=frozenset({BackendType.LOCAL_FS}),
        max_execution_time_s=30,
        compensation_tool_id="delete_file",
    )
    registry.register(spec)
    registry.register_handler("write_file", _write_file_handler)


def _write_file_handler(params: dict, workspace_path: str = None) -> dict:
    """
    params: {"path": str, "content": str}
    path 是相对于 workspace 的路径，或绝对路径。
    若提供 workspace_path，相对路径解析到 workspace，绝对路径必须在 workspace 内。
    """
    path = Path(params["path"])
    if workspace_path:
        ws = Path(workspace_path)
        if not path.is_absolute():
            path = ws / path
        else:
            try:
                path.relative_to(ws)
            except ValueError:
                raise PermissionError(f"write_file: path {path} is outside workspace {ws}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = params.get("content", "")
    path.write_text(content, encoding="utf-8")
    return {"written": str(path), "size": len(content)}
