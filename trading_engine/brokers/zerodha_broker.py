from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from trading_engine.core.broker import Bar, BaseBroker, OrderRequest, OrderResult, Position, Side
from trading_engine.core.rate_limiter import AsyncTokenBucket

IST = ZoneInfo("Asia/Kolkata")


class ZerodhaBroker(BaseBroker):
    """Kite Connect adapter. Capital and prices are denominated in INR."""
    name = "zerodha"

    def __init__(self, api_key: str, access_token: str, instrument_map: dict[str, tuple[str, int]],
                 *, limiter: AsyncTokenBucket | None = None):
        from kiteconnect import KiteConnect
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(access_token)
        self.instrument_map = instrument_map
        self.limiter = limiter or AsyncTokenBucket(2, 2)

    async def _call(self, fn, *args, **kwargs):
        await self.limiter.acquire()
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def get_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Day") -> list[Bar]:
        if symbol not in self.instrument_map:
            raise KeyError(f"No Kite instrument token configured for {symbol}")
        _, token = self.instrument_map[symbol]
        interval = "day" if timeframe.lower() in {"1day", "day", "1d"} else "60minute"
        rows = await self._call(self.kite.historical_data, token, start, end, interval)
        bars = []
        for row in rows:
            ts = row["date"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            bars.append(Bar(symbol=symbol, timestamp=ts, open=row["open"], high=row["high"],
                            low=row["low"], close=row["close"], volume=row.get("volume") or 0))
        return bars

    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        result = {}
        for offset in range(0, len(symbols), 200):
            names = symbols[offset:offset + 200]
            keys = [f"{self.instrument_map[symbol][0]}:{symbol}" for symbol in names if symbol in self.instrument_map]
            response = await self._call(self.kite.quote, keys) if keys else {}
            for symbol in names:
                item = response.get(f"{self.instrument_map[symbol][0]}:{symbol}") if symbol in self.instrument_map else None
                if item and item.get("last_price"):
                    result[symbol] = float(item["last_price"])
        return result

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        if order.symbol not in self.instrument_map:
            raise KeyError(f"No Kite instrument token configured for {order.symbol}")
        exchange, _ = self.instrument_map[order.symbol]
        kwargs = dict(variety=self.kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=order.symbol,
                      transaction_type=self.kite.TRANSACTION_TYPE_BUY if order.side == Side.BUY else self.kite.TRANSACTION_TYPE_SELL,
                      quantity=int(order.quantity), product=self.kite.PRODUCT_CNC,
                      order_type=self.kite.ORDER_TYPE_LIMIT if order.order_type == "limit" else self.kite.ORDER_TYPE_MARKET,
                      validity=self.kite.VALIDITY_DAY)
        if order.order_type == "limit":
            kwargs["price"] = order.limit_price
        response = await self._call(self.kite.place_order, **kwargs)
        return OrderResult(broker_order_id=str(response), client_order_id=order.client_order_id,
                           status="submitted", symbol=order.symbol, quantity=order.quantity,
                           side=order.side, submitted_at=datetime.now(timezone.utc),
                           raw_status="Kite order placement is not a fill confirmation")

    @staticmethod
    def _normalise_order(order: dict) -> dict:
        state = str(order.get("status", "")).lower()
        state = {"complete": "filled", "cancelled": "canceled"}.get(state, state)
        return {"id": str(order.get("order_id", "")), "state": state,
                "quantity": float(order.get("quantity") or 0),
                "cumulative_quantity": float(order.get("filled_quantity") or 0),
                "average_price": float(order.get("average_price") or 0),
                "price": float(order.get("price") or 0)}

    async def order_info(self, order_id: str) -> dict:
        history = await self._call(self.kite.order_history, order_id)
        return self._normalise_order(history[-1]) if history else {}

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        latest = {}
        while asyncio.get_running_loop().time() < deadline:
            latest = await self.order_info(order_id)
            if latest.get("state") in {"filled", "rejected", "cancelled", "canceled"}:
                return latest
            await asyncio.sleep(2)
        return latest

    async def place_stop_loss(self, symbol: str, quantity: float, stop_price: float) -> dict:
        exchange, _ = self.instrument_map[symbol]
        response = await self._call(self.kite.place_order, variety=self.kite.VARIETY_REGULAR,
                                    exchange=exchange, tradingsymbol=symbol,
                                    transaction_type=self.kite.TRANSACTION_TYPE_SELL,
                                    quantity=int(quantity), product=self.kite.PRODUCT_CNC,
                                    order_type=self.kite.ORDER_TYPE_SLM, trigger_price=round(stop_price, 2),
                                    validity=self.kite.VALIDITY_DAY)
        return {"id": str(response), "state": "open"}

    async def cancel_order(self, order_id: str) -> dict:
        response = await self._call(self.kite.cancel_order, self.kite.VARIETY_REGULAR, order_id)
        return {"id": order_id, "state": "cancel_requested", "response": str(response)}

    async def get_open_buy_commitment(self) -> float:
        orders = await self._call(self.kite.orders)
        active = {"open", "open pending", "validation pending", "put order req received", "modify pending"}
        total = 0.0
        for order in orders:
            if str(order.get("transaction_type", "")).upper() != "BUY" or str(order.get("status", "")).lower() not in active:
                continue
            remaining = float(order.get("pending_quantity") or 0)
            price = float(order.get("price") or order.get("trigger_price") or 0)
            if remaining and price <= 0:
                raise RuntimeError("Cannot value an open Kite buy order; refusing new entries")
            total += remaining * price
        return total

    async def get_positions(self) -> list[Position]:
        response = await self._call(self.kite.positions)
        return [Position(symbol=p["tradingsymbol"], quantity=float(p["quantity"]),
                         average_entry_price=float(p["average_price"] or p["last_price"]),
                         market_value=float(p["quantity"] * p["last_price"]))
                for p in response.get("net", []) if float(p.get("quantity") or 0) != 0]

    async def get_account_equity(self) -> float:
        margin = await self._call(self.kite.margins, "equity")
        positions = await self.get_positions()
        deployed = sum(abs(p.market_value) for p in positions)
        available_funds = float(margin.get("net") or margin.get("available", {}).get("live_balance") or 0)
        if available_funds <= 0 and deployed <= 0:
            raise RuntimeError("Kite returned no usable INR funds balance")
        return max(0.0, available_funds) + deployed

    async def close(self) -> None:
        return None
