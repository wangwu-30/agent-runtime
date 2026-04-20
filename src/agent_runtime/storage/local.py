"""
Storage — LocalFSBackend

本地文件系统实现。使用 rename 保证原子写入，fsync 保证持久化。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterator, Optional

from agent_runtime.contracts import BackendType
from agent_runtime.storage.base import StorageBackend


class LocalFSBackend(StorageBackend):

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def backend_type(self) -> BackendType:
        return BackendType.LOCAL_FS

    def _abs(self, path: str, create_parents: bool = False) -> Path:
        p = self._root / path
        if create_parents:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def read(self, path: str) -> Optional[bytes]:
        p = self._abs(path)
        if not p.exists():
            return None
        return p.read_bytes()

    def write_atomic(self, path: str, data: bytes) -> None:
        """rename-based 原子写入。"""
        target = self._abs(path, create_parents=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def append(self, path: str, data: bytes) -> None:
        """追加写入，fsync 保证持久化。"""
        target = self._abs(path, create_parents=True)
        with open(target, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def exists(self, path: str) -> bool:
        return self._abs(path).exists()  # create_parents=False (default), no side effects

    def list(self, prefix: str) -> Iterator[str]:
        base = self._root / prefix
        if not base.exists():
            return
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield str(p.relative_to(self._root))

    def delete_marker(self, path: str, reason: str) -> None:
        """写入 .deleted 标记文件，不删除原文件。(HC6)"""
        import json
        from datetime import datetime
        self.write_atomic(
            path + ".deleted",
            json.dumps({"reason": reason, "at": datetime.utcnow().isoformat()}).encode(),
        )
