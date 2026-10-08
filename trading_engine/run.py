"""Convenience entry point: python trading_engine/run.py robinhood"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from trading_engine.cli import main
except ModuleNotFoundError as exc:
    if exc.name in {"pydantic", "pydantic_settings", "pandas", "numpy", "scipy", "exchange_calendars"}:
        print("Trading dependencies are missing. Run: python -m pip install -r trading_engine/requirements.txt",
              file=sys.stderr)
        raise SystemExit(2) from exc
    raise


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0].lower() in {"status", "run"}:
        raise SystemExit(main(args))
    raise SystemExit(main(["run", *args]))
