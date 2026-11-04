"""測試共同設定與環境初始化。

確保專案根目錄加入 sys.path，使測試能正確匯入 monitor 模組。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 將專案根目錄加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
