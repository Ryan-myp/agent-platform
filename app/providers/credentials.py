from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


class ModelCredentialStore:
    """Local-only credential storage, kept separate from model metadata."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        with self._lock:
            if not self.path.exists():
                return {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError("本地模型凭据文件无法读取；为保护密钥，没有覆盖该文件。") from error
            if not isinstance(data, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in data.items()
            ):
                raise RuntimeError("本地模型凭据文件格式无效；为保护密钥，没有覆盖该文件。")
            return data

    def save(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(values, ensure_ascii=False, indent=2) + "\n"
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(serialized)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, self.path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
