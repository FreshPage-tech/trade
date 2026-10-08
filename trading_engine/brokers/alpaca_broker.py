from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from trading_engine.core.broker import Bar, BaseBroker, OrderRequest, OrderResult, Position, Side
from trading_engine.core.rate_limiter import AsyncTokenBucket


class AlpacaBroker(BaseBroker):
    """Alpaca adapter using alpaca-py; SDK calls run off the event loop."""
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, *, paper: bool = True,
                 limiter: AsyncTokenBucket | None = None):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        self.limiter = limiter or AsyncTokenBucket(2, 4)
        self.paper = paper

    async def _call(self, fn, *args, **kwargs):
        await self.limiter.acquire()
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def get_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Day") -> list[Bar]:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        tf = TimeFrame.Day if timeframe.lower() in {"1day", "day", "1d"} else TimeFrame.Hour
        response = await self._call(self.data.get_stock_bars,
                                    StockBarsRequest(symbol_or_symbols=symbol, timeframe=tf, start=start, end=end))
        return [Bar(symbol=symbol, timestamp=x.timestamp, open=x.open, high=x.high, low=x.low,
                    close=x.close, volume=x.volume) for x in response[symbol]]

    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        from alpaca.data.requests import StockLatestTradeRequest
        response = await self._call(self.data.get_stock_latest_trade,
                                    StockLatestTradeRequest(symbol_or_symbols=symbols))
        return {symbol: float(trade.price) for symbol, trade in response.items() if trade and trade.price}

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        side = OrderSide.BUY if order.side == Side.BUY else OrderSide.SELL
        args = dict(symbol=order.symbol, qty=order.quantity, side=side,
                    time_in_force=TimeInForce.DAY, client_order_id=order.client_order_id)
        if order.order_type == "limit":
            if order.limit_price is None:
                raise ValueError("limit_price is required for a limit order")
            request = LimitOrderRequest(**args, limit_price=order.limit_price)
        else:
            request = MarketOrderRequest(**args)
        response = await self._call(self.trading.submit_order, request)
        return OrderResult(broker_order_id=str(response.id), client_order_id=order.client_order_id,
                           status=str(response.status), symbol=order.symbol, quantity=float(response.qty),
                           side=order.side, submitted_at=response.submitted_at or datetime.now(timezone.utc),
                           average_fill_price=float(response.filled_avg_price) if response.filled_avg_price else None,
                           raw_status=str(response.status))

    @staticmethod
    def _order_dict(order) -> dict:
        return {"id": str(order.id), "state": str(order.status).lower(),
                "quantity": float(order.qty or 0), "cumulative_quantity": float(order.filled_qty or 0),
                "average_price": float(order.filled_avg_price or 0),
                "price": float(order.limit_price or 0) if order.limit_price else 0.0}

    async def order_info(self, order_id: str) -> dict:
        return self._order_dict(await self._call(self.trading.get_order_by_id, order_id))

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20) -> dict:
        from alpaca.trading.enums import OrderStatus
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        latest = {}
        while asyncio.get_running_loop().time() < deadline:
            latest = await self.order_info(order_id)
            if latest.get("state") in {str(x.value).lower() for x in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                                                        OrderStatus.EXPIRED, OrderStatus.REJECTED)}:
                return latest
            await asyncio.sleep(2)
        return latest

    async def place_stop_loss(self, symbol: str, quantity: float, stop_price: float) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        request = StopOrderRequest(symbol=symbol, qty=quantity, side=OrderSide.SELL,
                                   time_in_force=TimeInForce.GTC, stop_price=round(stop_price, 2),
                                   client_order_id=f"stop-{symbol}-{int(datetime.now().timestamp())}")
        response = await self._call(self.trading.submit_order, request)
        return {"id": str(response.id), "state": str(response.status).lower()}

    async def cancel_order(self, order_id: str) -> dict:
        response = await self._call(self.trading.cancel_order_by_id, order_id)
        return {"id": order_id, "state": "cancel_requested", "response": str(response)}

    async def get_open_buy_commitment(self) -> float:
        from alpaca.trading.enums import OrderSide, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = await self._call(self.trading.get_orders,
                                  GetOrdersRequest(status=QueryOrderStatus.OPEN))
        total = 0.0
        for order in orders:
            if order.side != OrderSide.BUY:
                continue
            remaining = max(0.0, float(order.qty or 0) - float(order.filled_qty or 0))
            price = float(order.limit_price or 0)
            if remaining and price <= 0:
                raise RuntimeError("Cannot value an open Alpaca buy order; refusing new entries")
            total += remaining * price
        return total

    async def get_positions(self) -> list[Position]:
        items = await self._call(self.trading.get_all_positions)
        return [Position(symbol=p.symbol, quantity=float(p.qty), average_entry_price=float(p.avg_entry_price),
                         market_value=float(p.market_value)) for p in items]

    async def get_account_equity(self) -> float:
        account = await self._call(self.trading.get_account)
        return float(account.equity)

    async def close(self) -> None:
        return None
