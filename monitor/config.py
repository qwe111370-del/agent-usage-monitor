"""唯讀載入專案設定；缺少或損壞的設定使用預設值。"""

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    local_interval_s: int = 30
    remote_interval_s: int = 180
    enabled: tuple[str, ...] = ("claude", "codex", "agy")
    claude_home: Path | None = None
    codex_home: Path | None = None
    agy_home: Path | None = None
    agy_bin: Path | None = None
    claude_desktop_dirs: list[Path] | None = None


def load_config(path: Path | None = None) -> Config:
    try:
        obj = json.loads((path if path is not None else Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
        if not isinstance(obj, dict):
            return Config()
        values = {}
        for item in fields(Config):
            name, value = item.name, obj.get(item.name)
            if name not in obj:
                continue
            if name in ("claude_home", "codex_home", "agy_home", "agy_bin"):
                if value is None or isinstance(value, str):
                    values[name] = Path(value) if value is not None else None
            elif name == "claude_desktop_dirs":
                if value is None or (isinstance(value, list) and all(isinstance(p, str) for p in value)):
                    values[name] = None if value is None else [Path(p) for p in value]
            elif name == "enabled":
                if isinstance(value, (list, tuple)) and all(isinstance(a, str) for a in value):
                    values[name] = tuple(a for a in value if a in ("claude", "codex", "agy"))
            elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
                values[name] = value
        return Config(**values)
    except (OSError, ValueError, TypeError):
        return Config()
