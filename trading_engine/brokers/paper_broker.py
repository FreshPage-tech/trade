from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from trading_engine.core.broker import Bar, BaseBroker, OrderRequest, OrderResult, Position, Side


class PaperBroker(BaseBroker):
    """Deterministic local broker; bars must be supplied by an approved feed or test fixture."""
    name = "paper"

    def __init__(self, starting_equity: float = 2000.0):
        self.equity = min(float(starting_equity), 2000.0)
        self._bars: dict[str, list[Bar]] = {}
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, OrderResult] = {}
        self._lock = asyncio.Lock()

    def set_bars(self, symbol: str, bars: list[Bar]) -> None:
        self._bars[symbol] = bars

    async def get_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Day") -> list[Bar]:
        return [b for b in self._bars.get(symbol, []) if start <= b.timestamp <= end]

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        async with self._lock:
            if order.client_order_id in self._orders:
                return self._orders[order.client_order_id]
            result = OrderResult(broker_order_id=f"paper-{order.client_order_id}", client_order_id=order.client_order_id,
                                status="accepted", symbol=order.symbol, quantity=order.quantity, side=order.side,
                                submitted_at=datetime.now(timezone.utc))
            self._orders[order.client_order_id] = result
            return result

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def get_account_equity(self) -> float:
        return min(self.equity, 2000.0)

    async def close(self) -> None:
        return None
