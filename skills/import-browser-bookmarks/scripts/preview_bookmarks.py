from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_SOURCE = REPOSITORY_ROOT / "services" / "api" / "src"
sys.path.insert(0, str(API_SOURCE))


if __name__ == "__main__":
    raise SystemExit(import_module("webhub.bookmarks.cli").main())
