"""Compatibility launcher for the routed daemon and its Telegram controls.

Use ``python run.py robinhood`` for broker selection. This file reads BROKER from
trading_engine/.env and starts the same daemon; it is not a second trading loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading_engine.main import main


if __name__ == "__main__":
    main()
