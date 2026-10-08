from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from trading_engine.core.broker import Bar, BaseBroker, OrderRequest, OrderResult, Position, Side
from trading_engine.core.rate_limiter import AsyncTokenBucket


class RobinhoodBroker(BaseBroker):
    """Robinhood stock adapter backed by robin_stocks (unofficial Robinhood API client).

    Entries use limit orders and exits use market orders. Protective stop orders are submitted separately after an
    entry fill is confirmed. Take-profit exits are monitored by the daemon.
    """
    name = "robinhood"

    def __init__(self, username: str, password: str, *, limiter: AsyncTokenBucket,
                 mfa_code: str | None = None, totp_secret: str | None = None):
        import robin_stocks.robinhood as rh
        self.rh = rh
        self.limiter = limiter
        self._instrument_symbols: dict[str, str] = {}
        self._short_positions_detected = False
        self._unpriced_positions: list[str] = []
        self._logged_in = False
        if not mfa_code and totp_secret:
            import pyotp
            mfa_code = pyotp.TOTP(totp_secret).now()
        response = rh.login(username=username, password=password, expiresIn=86400 * 30,
                            store_session=True, mfa_code=mfa_code)
        if not response:
            raise RuntimeError("Robinhood authentication failed; check session/MFA setup")
        self._logged_in = True

    async def _call(self, fn, *args, **kwargs):
        await self.limiter.acquire()
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def get_bars(self, symbol: str, start: datetime, end: datetime,
                       timeframe: str = "1Day") -> list[Bar]:
        if timeframe.lower() not in {"1day", "day", "1d"}:
            raise ValueError("Robinhood adapter currently supports daily historical bars only")
        rows = await self._call(self.rh.stocks.get_stock_historicals, symbol,
                                interval="day", span="5year", bounds="regular")
        result = []
        for row in rows or []:
            if not row or not row.get("begins_at"):
                continue
            bar = Bar(symbol=symbol, timestamp=row["begins_at"],
                      open=float(row["open_price"]), high=float(row["high_price"]),
                      low=float(row["low_price"]), close=float(row["close_price"]),
                      volume=float(row.get("volume") or 0))
            if start <= bar.timestamp <= end:
                result.append(bar)
        return result

    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        result: dict[str, float] = {}
        # One account/data request per bounded batch to keep traffic low.
        for offset in range(0, len(symbols), 50):
            rows = await self._call(self.rh.get_quotes, symbols[offset:offset + 50])
            for row in rows or []:
                if row and row.get("symbol") and row.get("last_trade_price"):
                    result[str(row["symbol"])] = float(row["last_trade_price"])
        return result

    async def get_quote(self, symbol: str) -> float:
        rows = await self.get_quotes([symbol])
        if symbol not in rows:
            raise RuntimeError(f"Robinhood returned no usable quote for {symbol}")
        return rows[symbol]

    async def submit_order(self, order: OrderRequest) -> OrderResult:
        if order.order_type == "limit":
            if order.limit_price is None:
                raise ValueError("limit_price is required for a limit order")
            fn = self.rh.orders.order_buy_limit if order.side == Side.BUY else self.rh.orders.order_sell_limit
            response = await self._call(fn, order.symbol, int(order.quantity), float(order.limit_price), timeInForce="gfd")
        else:
            fn = self.rh.orders.order_buy_market if order.side == Side.BUY else self.rh.orders.order_sell_market
            response = await self._call(fn, order.symbol, int(order.quantity), timeInForce="gfd")
        if not isinstance(response, dict) or not response.get("id"):
            raise RuntimeError(f"Robinhood rejected {order.side.value} order for {order.symbol}")
        return OrderResult(broker_order_id=str(response["id"]), client_order_id=order.client_order_id,
                           status=str(response.get("state", "submitted")), symbol=order.symbol,
                           quantity=order.quantity, side=order.side,
                           submitted_at=datetime.now(timezone.utc),
                           average_fill_price=float(response["average_price"]) if response.get("average_price") else None,
                           raw_status=str(response.get("state", "submitted")))

    async def order_info(self, order_id: str) -> dict:
        response = await self._call(self.rh.orders.get_stock_order_info, order_id)
        return response if isinstance(response, dict) else {}

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        latest: dict = {}
        while asyncio.get_running_loop().time() < deadline:
            latest = await self.order_info(order_id)
            state = str(latest.get("state", "")).lower()
            if state in {"filled", "rejected", "failed", "canceled", "cancelled"}:
                return latest
            await asyncio.sleep(2)
        return latest

    async def place_stop_loss(self, symbol: str, quantity: float, stop_price: float) -> dict:
        response = await self._call(self.rh.orders.order_sell_stop_loss, symbol,
                                    int(quantity), round(float(stop_price), 2), timeInForce="gtc")
        if not isinstance(response, dict) or not response.get("id"):
            raise RuntimeError(f"Robinhood rejected protective stop for {symbol}")
        return response

    async def cancel_order(self, order_id: str) -> dict:
        response = await self._call(self.rh.orders.cancel_stock_order, order_id)
        return response if isinstance(response, dict) else {}

    async def get_positions(self) -> list[Position]:
        raw_positions = await self._call(self.rh.account.get_open_stock_positions)
        parsed: list[tuple[str, float, float]] = []
        self._short_positions_detected = False
        self._unpriced_positions = []
        for raw in raw_positions or []:
            instrument = raw.get("instrument", "")
            if instrument not in self._instrument_symbols:
                try:
                    symbol = await self._call(self.rh.stocks.get_symbol_by_url, instrument)
                except Exception:
                    symbol = None
                if not symbol:
                    self._unpriced_positions.append("unmapped stock position")
                    continue
                self._instrument_symbols[instrument] = str(symbol)
            symbol = self._instrument_symbols[instrument]
            quantity = float(raw.get("quantity") or 0)
            average = float(raw.get("average_buy_price") or 0)
            if quantity < 0:
                self._short_positions_detected = True
            if quantity != 0 and average > 0:
                parsed.append((symbol, quantity, average))
        positions: list[Position] = []
        price_by_symbol = {}
        if parsed:
            try:
                quotes = await self._call(self.rh.get_quotes, [item[0] for item in parsed])
            except Exception:
                quotes = []
            price_by_symbol = {q.get("symbol"): float(q["last_trade_price"])
                               for q in (quotes or []) if q and q.get("symbol") and q.get("last_trade_price")}
            for symbol, quantity, average in parsed:
                mark = price_by_symbol.get(symbol)
                if mark is None:
                    mark = average
                    self._unpriced_positions.append(symbol)
                positions.append(Position(symbol=symbol, quantity=quantity, average_entry_price=average,
                                          market_value=quantity * mark))
        # Crypto is not traded by this strategy, but it still consumes account capital
        # and must count against the engine-wide exposure ceiling.
        crypto_positions = await self._call(self.rh.crypto.get_crypto_positions)
        for raw in crypto_positions or []:
            quantity = float(raw.get("quantity_available") or raw.get("quantity") or 0)
            currency = raw.get("currency") or {}
            code = currency.get("code") if isinstance(currency, dict) else None
            if quantity <= 0 or not code:
                continue
            try:
                quote = await self._call(self.rh.crypto.get_crypto_quote, code)
                mark = float((quote or {}).get("mark_price") or 0)
            except Exception:
                mark = 0.0
            if mark <= 0:
                cost_basis = float(raw.get("cost_basis") or 0)
                mark = cost_basis / quantity if cost_basis > 0 else 0.0
                self._unpriced_positions.append(f"CRYPTO:{code}")
            positions.append(Position(symbol=f"CRYPTO:{code}", quantity=quantity,
                                      average_entry_price=mark, market_value=quantity * mark))
        return positions

    async def get_risk_warnings(self) -> list[str]:
        warnings = []
        if self._short_positions_detected:
            warnings.append("Short stock positions are not supported by the strategy risk model")
        if self._unpriced_positions:
            warnings.append("Current quotes unavailable for: " + ", ".join(self._unpriced_positions[:10]))
        options = await self._call(self.rh.options.get_open_option_positions)
        option_count = sum(1 for row in options or [] if row and float(row.get("quantity") or 0) > 0)
        if option_count:
            warnings.append(f"{option_count} open option position(s) are not valued by the engine")
        option_orders = await self._call(self.rh.orders.get_all_open_option_orders)
        if any(row and str(row.get("side", "")).lower() == "buy" for row in (option_orders or [])):
            warnings.append("Open option buy orders are not valued by the engine")
        crypto_orders = await self._call(self.rh.orders.get_all_open_crypto_orders)
        if any(row and str(row.get("side", "")).lower() == "buy" for row in (crypto_orders or [])):
            warnings.append("Open crypto buy orders are not valued by the engine")
        return warnings

    async def get_open_buy_commitment(self) -> float:
        orders = await self._call(self.rh.orders.get_all_open_stock_orders)
        commitment = 0.0
        for order in orders or []:
            if not order or str(order.get("side", "")).lower() != "buy":
                continue
            quantity = float(order.get("quantity") or 0) - float(order.get("cumulative_quantity") or 0)
            if quantity <= 0:
                continue
            price = float(order.get("price") or order.get("stop_price") or 0)
            if price <= 0:
                raise RuntimeError("Could not value an open Robinhood buy order; refusing risk allocation")
            commitment += quantity * price
        return commitment

    async def get_account_equity(self) -> float:
        portfolio = await self._call(self.rh.profiles.load_portfolio_profile)
        account = await self._call(self.rh.profiles.load_account_profile)
        # Robinhood portfolio equity is the best available total; use buying power only
        # as a fallback and never let the engine allocate above its own hard cap.
        raw = (portfolio or {}).get("equity") or (account or {}).get("buying_power") or (account or {}).get("cash")
        if raw is None:
            raise RuntimeError("Robinhood returned no account equity or buying power")
        return float(raw)

    async def close(self) -> None:
        # Keep the persisted Robinhood session available for the next daemon restart.
        return None
