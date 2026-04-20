"""
Storage — StorageBackend 抽象接口

所有存储操作通过此接口，本地和云端实现相同契约。(ADR-002)
trace 存储必须满足强一致性，S3 禁止用于 trace。(ADR-016)
"""

from __future__ import annotations

import abc
from typing import Iterator, Optional

from agent_runtime.contracts import BackendType, TraceBackendError, TRACE_ALLOWED_BACKENDS


class StorageBackend(abc.ABC):
    """最小存储契约。(ADR-002)"""

    @property
    @abc.abstractmethod
    def backend_type(self) -> BackendType:
        ...

    @abc.abstractmethod
    def read(self, path: str) -> Optional[bytes]:
        """读取文件内容，不存在返回 None。"""
        ...

    @abc.abstractmethod
    def write_atomic(self, path: str, data: bytes) -> None:
        """原子写入：要么完全成功，要么完全失败。本地用 rename，GCS 用条件写。"""
        ...

    @abc.abstractmethod
    def append(self, path: str, data: bytes) -> None:
        """追加写入。trace 文件专用，必须是强一致的。"""
        ...

    @abc.abstractmethod
    def exists(self, path: str) -> bool:
        ...

    @abc.abstractmethod
    def list(self, prefix: str) -> Iterator[str]:
        """列出 prefix 下的所有路径。"""
        ...

    @abc.abstractmethod
    def delete_marker(self, path: str, reason: str) -> None:
        """逻辑删除，不物理删除。(HC6 历史不可删除)"""
        ...

    def assert_trace_safe(self) -> None:
        """trace 存储必须满足强一致性约束。(ADR-016)"""
        if self.backend_type not in TRACE_ALLOWED_BACKENDS:
            raise TraceBackendError(
                f"Backend {self.backend_type.value} is not allowed for trace storage. "
                f"Allowed: {[b.value for b in TRACE_ALLOWED_BACKENDS]}. "
                f"S3 uses eventual consistency and is explicitly prohibited."
            )
