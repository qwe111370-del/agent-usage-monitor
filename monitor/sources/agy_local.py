"""唯讀解析 Antigravity 對話 SQLite 與無 schema protobuf 用量。"""

from pathlib import Path
import sqlite3

from ..model import TokenCounts, UsageEvent


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("protobuf 長度不足")
        value = data[offset]
        offset += 1
        if shift == 63 and value > 1:
            raise ValueError("protobuf 整數超出範圍")
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
    raise ValueError("protobuf 整數超出範圍")


def _fields(data: bytes) -> list[tuple[int, int, int | bytes]]:
    fields = []
    offset = 0
    while offset < len(data):
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0 or number > 536870911:
            raise ValueError("protobuf 欄位無效")
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - offset:
                raise ValueError("protobuf 長度不足")
            value = data[offset:offset + size]
            offset += size
        else:
            raise ValueError("protobuf wire type 不支援")
        fields.append((number, wire, value))
    return fields


def _first(fields, number, wire):
    return next((value for field, kind, value in fields if field == number and kind == wire), None)


def decode_usage(blob: bytes) -> TokenCounts | None:
    try:
        first = _first(_fields(blob), 1, 2)
        nested = _first(_fields(first), 4, 2)
        values = _fields(nested)
        counts = {number: _first(values, number, 0) or 0 for number in (2, 3, 5, 9)}
        return TokenCounts(input=counts[2], output=counts[3] + counts[9], cache_read=counts[5])
    except Exception:
        return None


def decode_step_time(blob: bytes) -> float | None:
    try:
        nested = _first(_fields(blob), 1, 2)
        result = _first(_fields(nested), 1, 0)
        return float(result) if result is not None else None
    except Exception:
        return None


class AgyConversationReader:
    def __init__(self, conversations_dir: Path):
        self.conversations_dir = Path(conversations_dir)
        self._files: dict[Path, tuple[tuple, UsageEvent]] = {}

    def refresh(self) -> None:
        try:
            paths = sorted(self.conversations_dir.glob("*.db"))
        except OSError:
            return
        for path in list(self._files):
            if path not in paths:
                del self._files[path]
        for path in paths:
            try:
                stat = path.stat()
                wal = Path(str(path) + "-wal")
                try:
                    wal_stat = wal.stat()
                    wal_signature = (wal_stat.st_size, wal_stat.st_mtime_ns)
                except FileNotFoundError:
                    wal_signature = None
                signature = (stat.st_size, stat.st_mtime_ns, wal_signature)
                if path in self._files and self._files[path][0] == signature:
                    continue
                counts = TokenCounts()
                timestamps = []
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
                try:
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    for (blob,) in db.execute("SELECT data FROM gen_metadata"):
                        value = decode_usage(blob)
                        if value is not None:
                            counts = counts + value
                    for (blob,) in db.execute("SELECT metadata FROM steps"):
                        ts = decode_step_time(blob)
                        if ts is not None:
                            timestamps.append(ts)
                finally:
                    db.close()
                self._files[path] = (signature, UsageEvent(max(timestamps) if timestamps else stat.st_mtime, counts))
            except (OSError, sqlite3.Error):
                # 不更新簽章，確保暫時鎖住或資料未齊時，下次仍會重試。
                continue

    @property
    def events(self) -> list[UsageEvent]:
        return sorted((value[1] for value in self._files.values()), key=lambda event: event.ts)
