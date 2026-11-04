"""保存畫面模式、兩種模式的位置與透明度；懸浮視窗可拖曳、雙擊還原或按右鍵操作。"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "ui_settings.json"


@dataclass
class UISettings:
    """一般視窗與懸浮視窗各自保留位置。"""

    mode: str = "normal"
    normal_position: tuple[int, int] | None = None
    compact_position: tuple[int, int] | None = None
    alpha: float = 0.92
    normal_topmost: bool = False


def load_settings(path: Path | None = None) -> UISettings:
    """讀取畫面設定；缺檔、損毀或不合法的欄位使用預設值。"""
    settings = UISettings()
    try:
        data = json.loads((path or DEFAULT_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return settings
    if not isinstance(data, dict):
        return settings
    if data.get("mode") in ("normal", "compact"):
        settings.mode = data["mode"]
    for name in ("normal_position", "compact_position"):
        value = data.get(name)
        if (isinstance(value, list) and len(value) == 2
                and all(type(n) is int and abs(n) <= 100_000 for n in value)):
            setattr(settings, name, tuple(value))
    alpha = data.get("alpha")
    if (type(alpha) in (int, float) and math.isfinite(alpha)
            and 0.7 <= alpha <= 1.0):
        settings.alpha = float(alpha)
    if type(data.get("normal_topmost")) is bool:
        settings.normal_topmost = data["normal_topmost"]
    return settings


def save_settings(settings: UISettings, path: Path | None = None) -> bool:
    """以暫存檔原子取代設定；無法寫入時回傳 False，避免中斷視窗操作。"""
    target = path or DEFAULT_PATH
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                prefix=target.name + ".", suffix=".tmp",
                                delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(settings), stream, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
