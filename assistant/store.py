"""Small file-backed stores kept under ``data/`` (a Railway Volume in production)."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]


def ensure_private_dir(path: PathLike) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def append_private_line(path: PathLike, line: str) -> None:
    """Append one line to a file that is created with mode 0600."""
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")


class JsonFile:
    """A JSON document with atomic writes. A missing or broken file reads as the default."""

    def __init__(self, path: PathLike, default: Any) -> None:
        self.path = Path(path)
        self._default = default

    def read(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return copy.deepcopy(self._default)

    def write(self, data: Any) -> None:
        ensure_private_dir(self.path.parent)
        descriptor, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise


class HistoryStore:
    """Recent messages, kept separately per business connection and chat."""

    def __init__(self, path: PathLike, limit: int) -> None:
        self._file = JsonFile(path, {})
        self._limit = limit
        loaded = self._file.read()
        self._data: Dict[str, List[Dict[str, Any]]] = loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._data.get(self._key(connection_id, chat_id), []))

    def add(
        self,
        connection_id: str,
        chat_id: int,
        *,
        role: str,
        name: str,
        text: str,
        lang: Optional[str],
        ts: float,
    ) -> None:
        items = self._data.setdefault(self._key(connection_id, chat_id), [])
        items.append({"role": role, "name": name, "text": text, "lang": lang, "ts": ts})
        del items[: -self._limit]
        self._file.write(self._data)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self._data.get(self._key(connection_id, chat_id), [])):
            if item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self._data)


class SeenMessages:
    """Remembers handled message keys across restarts so duplicates are dropped."""

    def __init__(self, path: PathLike, capacity: int = 5000) -> None:
        self._file = JsonFile(path, [])
        self._capacity = capacity
        loaded = self._file.read()
        keys = loaded if isinstance(loaded, list) else []
        self._keys: "OrderedDict[str, None]" = OrderedDict((str(key), None) for key in keys)

    def first_time(self, key: str) -> bool:
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self._capacity:
            self._keys.popitem(last=False)
        self._file.write(list(self._keys))
        return True


class RuntimeState:
    """Pause flag and usage-limit deadline, kept across restarts."""

    def __init__(self, path: PathLike) -> None:
        self._file = JsonFile(path, {})
        loaded = self._file.read()
        self._data: Dict[str, Any] = loaded if isinstance(loaded, dict) else {}

    @property
    def paused(self) -> bool:
        return bool(self._data.get("paused", False))

    @paused.setter
    def paused(self, value: bool) -> None:
        self._data["paused"] = bool(value)
        self._file.write(self._data)

    @property
    def limit_until(self) -> Optional[float]:
        value = self._data.get("limit_until")
        return float(value) if isinstance(value, (int, float)) else None

    @limit_until.setter
    def limit_until(self, value: Optional[float]) -> None:
        self._data["limit_until"] = value
        self._file.write(self._data)
