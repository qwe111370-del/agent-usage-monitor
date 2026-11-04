"""增量讀取 Claude JSONL 用量，處理半行、檔案更換與跨檔去重。"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

from ..model import TokenCounts, UsageEvent


def _timestamp(value) -> float | None:
    try:
        if not isinstance(value, str):
            return None
        result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _number(value, default=0):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return value if math.isfinite(value) else default
        except OverflowError:
            pass
    return default


def _count(value) -> int:
    return max(0, int(_number(value)))


def parse_line(line: str) -> tuple[tuple | None, UsageEvent] | None:
    try:
        obj = json.loads(line)
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            return None
        message = obj.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            return None
        ts = _timestamp(obj.get("timestamp"))
        if ts is None:
            return None
        usage = message["usage"]
        ids = (message.get("id"), obj.get("requestId"))
        key = ids if any(ids) else None
        if key is not None:
            hash(key)
        return key, UsageEvent(ts, TokenCounts(
            input=_count(usage.get("input_tokens")),
            output=_count(usage.get("output_tokens")),
            cache_write=_count(usage.get("cache_creation_input_tokens")),
            cache_read=_count(usage.get("cache_read_input_tokens")),
        ))
    except (ValueError, TypeError, OverflowError, RecursionError):
        return None


class _JsonlReader:
    """兩家 JSONL 共用的二進位增量讀取；來源各自保管解析狀態。"""

    def __init__(self, roots: list[Path], pattern: str, keep_days: float):
        self.roots = [Path(root) for root in roots]
        self.pattern = pattern
        self.keep_days = keep_days
        self._files: dict[Path, dict] = {}
        self._now = time.time()
        self._serial = 0

    def _new_data(self) -> dict:
        return {"records": []}

    def _consume(self, state: dict, line: str) -> None:
        record = parse_line(line)
        if record is not None:
            self._serial += 1
            state["records"].append((*record, self._serial))

    def refresh(self, now: float | None = None) -> None:
        self._now = time.time() if now is None else now
        cutoff = self._now - self.keep_days * 86400
        seen = set()
        for root in self.roots:
            try:
                paths = sorted(root.rglob(self.pattern))
            except OSError:
                continue
            for path in paths:
                try:
                    stat = path.stat()
                    if not path.is_file() or stat.st_mtime < cutoff:
                        continue
                    seen.add(path)
                    old = self._files.get(path)
                    identity = (stat.st_dev, stat.st_ino)
                    reset = (old is None or old["identity"] != identity
                             or stat.st_size < old["offset"]
                             or (stat.st_size == old["size"] and stat.st_mtime_ns != old["mtime"]))
                    if not reset and stat.st_size == old["size"] and stat.st_mtime_ns == old["mtime"]:
                        continue
                    # 先成功開啟及讀取，再替換舊結果；暫時讀取失敗時保留資料。
                    offset = 0 if reset else old["offset"]
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        added = stream.read()
                        offset = stream.tell()
                    state = self._new_data() if reset else old
                    pending = b"" if reset else state["pending"]
                    lines = (pending + added).split(b"\n")
                    state.update(identity=identity, size=stat.st_size, mtime=stat.st_mtime_ns,
                                 offset=offset, pending=lines.pop())
                    for line in lines:
                        self._consume(state, line.decode("utf-8", errors="replace"))
                    self._files[path] = state
                except OSError:
                    if path in self._files:
                        seen.add(path)
        for path in list(self._files):
            if path not in seen:
                del self._files[path]
        for state in self._files.values():
            state["records"] = [record for record in state["records"] if record[1].ts >= cutoff]


class ClaudeLogReader(_JsonlReader):
    def __init__(self, projects_dir: Path, keep_days: float = 8):
        super().__init__([projects_dir], "*.jsonl", keep_days)

    @property
    def events(self) -> list[UsageEvent]:
        seen = set()
        events = []
        records = sorted((record for state in self._files.values() for record in state["records"]), key=lambda record: record[2])
        for key, event, _ in records:
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            events.append(event)
        return sorted(events, key=lambda event: event.ts)
