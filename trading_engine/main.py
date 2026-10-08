from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import signal
import uuid
from datetime import datetime, time as clock_time, timedelta, timezone

import exchange_calendars as xcals
import pandas as pd

from trading_engine.brokers.factory import build_broker
from trading_engine.config.settings import get_settings
from trading_engine.core.rate_limiter import AsyncTokenBucket
from trading_engine.core.risk_manager import RiskManager
from trading_engine.core.broker import OrderRequest, Side
from trading_engine.storage.db import SQLiteStore
from trading_engine.strategies.backtester import rolling_backtest
from trading_engine.strategies.screener import screen_symbol
from trading_engine.trade import evaluate_setup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("trading_engine")


class TradingDaemon:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self.capital_limit = (self.settings.kite_capital_limit_inr if self.settings.broker == "zerodha"
                              else self.settings.capital_limit_usd)
        self.capital_unit = "INR" if self.settings.broker == "zerodha" else "USD"
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.settings.database_path.with_suffix(self.settings.database_path.suffix + ".lock")
        self._instance_lock = lock_path.open("a+")
        try:
            fcntl.flock(self._instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another trading_engine process already holds the account lock") from exc
        self.store = SQLiteStore(self.settings.database_path)
        self.broker = build_broker(self.settings)
        self.entries_halted = bool(self.store.get_runtime_state("entries_halted", False))
        self.last_cycle_at: datetime | None = None
        self.risk = RiskManager(self.capital_limit, self.settings.per_trade_risk_fraction,
                                self.settings.max_open_positions)
        self.stop = asyncio.Event()
        self.calendar = xcals.get_calendar("XBOM" if self.settings.broker == "zerodha" else "XNYS")
        self.last_scan_session: str | None = self.store.get_runtime_state("last_scan_session")
        last_scan_at = self.store.get_runtime_state("last_scan_at")
        self.last_scan_at = datetime.fromisoformat(last_scan_at) if last_scan_at else None
        self.last_scan_summary: dict = self.store.get_runtime_state("last_scan_summary", {})
        self.backtest_cache: dict[str, tuple[tuple, object]] = {}
        self.telegram_app = None

    def set_entries_halted(self, halted: bool) -> None:
        self.entries_halted = bool(halted)
        self.store.set_runtime_state("entries_halted", self.entries_halted)
        self.store.audit("telegram_entries_halted" if halted else "telegram_entries_resumed", {
            "broker": self.settings.broker,
        })

    async def notify_telegram(self, message: str) -> bool:
        if self.telegram_app is None or not self.settings.telegram_authorized_user_id:
            return False
        try:
            await self.telegram_app.bot.send_message(
                chat_id=self.settings.telegram_authorized_user_id, text=message)
            return True
        except Exception:
            log.exception("Could not deliver Telegram notification")
            return False

    async def notify_position_changes(self, positions: list) -> None:
        current = {position.symbol: float(position.quantity) for position in positions}
        previous = self.store.get_runtime_state("broker_position_snapshot")
        self.store.set_runtime_state("broker_position_snapshot", current)
        if previous is None:
            return
        managed_symbols = {trade["symbol"] for trade in self.store.managed_trades(status=None)
                           if trade["status"] != "closed"}
        changes = []
        for symbol in sorted(set(previous) | set(current)):
            before, after = previous.get(symbol), current.get(symbol)
            if before == after or symbol in managed_symbols:
                continue
            if after is None:
                changes.append(f"{symbol} closed/removed manually (was {before:g})")
            elif before is None:
                changes.append(f"{symbol} opened externally (now {after:g})")
            else:
                changes.append(f"{symbol} changed externally ({before:g} to {after:g})")
        if changes:
            self.store.audit("external_position_change", {"changes": changes})
            await self.notify_telegram("BROKER ACCOUNT CHANGE DETECTED\n" + "\n".join(changes[:12])
                                       + "\nUse /review to compare against the plan.")

    def universe(self) -> list[str]:
        with self.settings.universe_path.open(encoding="utf-8") as f:
            data = json.load(f)
        if self.settings.broker == "zerodha":
            return list(self.broker.instrument_map)
        # Current US strategy path uses listed equities and ETFs; crypto is exposure-only.
        return list(dict.fromkeys(data.get("equities", []) + data.get("etfs", [])))

    def is_open(self, now: datetime, session) -> bool:
        return self.calendar.session_open(session).to_pydatetime() <= now < self.calendar.session_close(session).to_pydatetime()

    async def _finish_pending_exit(self, trade: dict) -> None:
        symbol = trade["symbol"]
        status = trade["status"]
        if status == "entry_pending":
            info = await self.broker.order_info(trade["entry_order_id"])
            state = str(info.get("state", "")).lower()
            terminal = state in {"filled", "rejected", "failed", "canceled", "cancelled"}
            if not terminal and datetime.now(timezone.utc) - datetime.fromisoformat(trade["opened_at"]) > timedelta(seconds=30):
                await self.broker.cancel_order(trade["entry_order_id"])
                return
            if not terminal:
                return
            filled = float(info.get("cumulative_quantity") or (info.get("quantity") if state == "filled" else 0) or 0)
            if filled <= 0:
                self.store.update_managed_trade(symbol, status="closed")
                self.store.audit("entry_cancelled_unfilled", {"symbol": symbol, "state": state})
                return
            fill_price = float(info.get("average_price") or info.get("price") or trade["entry_price"])
            self.store.update_managed_trade(symbol, status="stop_submit_pending", quantity=filled, entry_price=fill_price)
            trade["status"] = "stop_submit_pending"
            trade["quantity"] = filled
            trade["entry_price"] = fill_price

        if status == "manual_close_cancel_pending":
            stop_id = trade.get("stop_order_id")
            if not stop_id:
                self.store.update_managed_trade(symbol, status="closed")
                return
            info = await self.broker.order_info(stop_id)
            state = str(info.get("state", "")).lower()
            if state == "filled":
                self.store.update_managed_trade(symbol, status="closed")
                self.store.audit("manual_close_stop_already_filled", {"symbol": symbol, "order_id": stop_id})
                return
            if state not in {"canceled", "cancelled", "rejected", "failed", "expired"}:
                await self.broker.cancel_order(stop_id)
                info = await self.broker.order_info(stop_id)
                state = str(info.get("state", "")).lower()
            if state in {"canceled", "cancelled", "rejected", "failed", "expired"}:
                self.store.update_managed_trade(symbol, status="closed")
                self.store.audit("manual_close_stop_canceled", {"symbol": symbol, "order_id": stop_id})
            else:
                log.warning("Manual close detected for %s; waiting for protective stop cancellation", symbol)
            return

        if status == "stop_submit_pending" or trade["status"] == "stop_submit_pending":
            stop = await self.broker.place_stop_loss(symbol, trade["quantity"], trade["stop_price"])
            self.store.update_managed_trade(symbol, status="open", stop_order_id=str(stop["id"]))
            self.store.audit("protective_stop_recovered", {"symbol": symbol, "order_id": stop["id"]})
            return

        if status == "emergency_exit_pending":
            info = await self.broker.order_info(trade["exit_order_id"])
            state = str(info.get("state", "")).lower()
            if state == "filled":
                self.store.update_managed_trade(symbol, status="closed")
            elif state in {"rejected", "failed", "canceled", "cancelled"}:
                stop = await self.broker.place_stop_loss(symbol, trade["quantity"], trade["stop_price"])
                self.store.update_managed_trade(symbol, status="open", stop_order_id=str(stop["id"]), exit_order_id=None)
            return

        if status == "target_cancel_pending":
            info = await self.broker.order_info(trade["stop_order_id"])
            state = str(info.get("state", "")).lower()
            if state == "filled":
                self.store.update_managed_trade(symbol, status="closed")
                self.store.audit("stop_filled", {"symbol": symbol, "order_id": trade["stop_order_id"]})
                await self.notify_telegram(f"STOP FILLED: {symbol}. Managed position marked closed.")
                return
            if state not in {"canceled", "cancelled", "rejected", "failed"}:
                await self.broker.cancel_order(trade["stop_order_id"])
                return
            self.store.update_managed_trade(symbol, status="target_cancelled")
            status = "target_cancelled"

        if status == "target_exit_pending":
            info = await self.broker.order_info(trade["exit_order_id"])
            state = str(info.get("state", "")).lower()
            if state == "filled":
                self.store.update_managed_trade(symbol, status="closed")
                self.store.audit("target_filled", {"symbol": symbol, "order_id": trade["exit_order_id"]})
                await self.notify_telegram(f"TARGET FILLED: {symbol}. Managed position marked closed.")
            elif state in {"rejected", "failed", "canceled", "cancelled"}:
                # The position still exists; restore its protective stop before retrying later.
                stop = await self.broker.place_stop_loss(symbol, trade["quantity"], trade["stop_price"])
                self.store.update_managed_trade(symbol, status="open", stop_order_id=str(stop["id"]), exit_order_id=None)
            return

        if status == "target_cancelled":
            # A restart can occur after the broker accepted the target exit but before its ID was saved.
            # Confirm the position still exists before ever issuing another sell.
            positions = await self.broker.get_positions()
            current = next((p for p in positions if p.symbol == symbol), None)
            if current is None or current.quantity <= 0:
                self.store.update_managed_trade(symbol, status="closed")
                self.store.audit("target_position_already_closed", {"symbol": symbol})
                return
            result = await self.broker.submit_order(OrderRequest(
                symbol=symbol, side=Side.SELL, quantity=trade["quantity"],
                client_order_id=f"target-{symbol}-{uuid.uuid4().hex[:12]}"))
            self.store.update_managed_trade(symbol, status="target_exit_pending", exit_order_id=result.broker_order_id)

    async def manage_open_trades(self, quotes: dict[str, float], positions: list) -> None:
        held = {p.symbol: p for p in positions}
        for trade in self.store.managed_trades(status=None):
            if trade["status"] == "closed":
                continue
            symbol = trade["symbol"]
            try:
                if trade["status"] in {"entry_pending", "stop_submit_pending", "emergency_exit_pending", "target_cancel_pending", "target_cancelled", "target_exit_pending"}:
                    await self._finish_pending_exit(trade)
                    continue
                if trade["status"] == "manual_close_cancel_pending":
                    await self._finish_pending_exit(trade)
                    continue
                if symbol not in held:
                    self.store.update_managed_trade(symbol, status="manual_close_cancel_pending")
                    self.store.audit("managed_position_missing", {"symbol": symbol,
                                                                   "action": "cancel_protective_stop"})
                    await self.notify_telegram(
                        f"RECONCILIATION: {symbol} is no longer in the broker account. "
                        "Canceling its old protective stop and marking the managed trade closed."
                    )
                    await self._finish_pending_exit({**trade, "status": "manual_close_cancel_pending"})
                    continue
                actual_quantity = abs(float(held[symbol].quantity))
                if abs(actual_quantity - float(trade["quantity"])) > 1e-6:
                    if trade["status"] != "quantity_review_required":
                        self.store.update_managed_trade(symbol, status="quantity_review_required")
                        self.set_entries_halted(True)
                        self.store.audit("managed_quantity_changed_externally", {
                            "symbol": symbol, "managed_quantity": trade["quantity"],
                            "broker_quantity": actual_quantity,
                        })
                        await self.notify_telegram(
                            f"RECONCILIATION REQUIRED: {symbol} quantity changed manually "
                            f"({trade['quantity']:g} managed vs {actual_quantity:g} at broker). "
                            "New entries are halted; review the stop and position in Robinhood."
                        )
                    continue
                if trade["status"] == "quantity_review_required":
                    continue
                stop_info = await self.broker.order_info(trade["stop_order_id"])
                stop_state = str(stop_info.get("state", "")).lower()
                if stop_state == "filled":
                    self.store.update_managed_trade(symbol, status="closed")
                    self.store.audit("stop_filled", {"symbol": symbol, "order_id": trade["stop_order_id"]})
                    await self.notify_telegram(f"STOP FILLED: {symbol}. Managed position marked closed.")
                    continue
                if stop_state in {"canceled", "cancelled", "rejected", "failed"}:
                    # Restore protection immediately if the broker no longer has the stop active.
                    stop = await self.broker.place_stop_loss(symbol, trade["quantity"], trade["stop_price"])
                    self.store.update_managed_trade(symbol, stop_order_id=str(stop["id"]))
                    continue
                price = quotes.get(symbol)
                if price is not None and price >= float(trade["target_price"]):
                    # Persist intent before canceling the stop so restarts can reconcile the sequence.
                    self.store.update_managed_trade(symbol, status="target_cancel_pending")
                    trade["status"] = "target_cancel_pending"
                    await self._finish_pending_exit(trade)
            except Exception:
                log.exception("Could not reconcile managed position %s; leaving broker-side stop in place", symbol)

    async def enter_candidate(self, candidate) -> None:
        symbol = candidate.symbol
        request = OrderRequest(symbol=symbol, side=Side.BUY, quantity=candidate.bracket.quantity,
                               order_type="limit", limit_price=candidate.bracket.entry,
                               client_order_id=f"entry-{symbol}-{uuid.uuid4().hex[:12]}")
        result = await self.broker.submit_order(request)
        pending_trade = {"symbol": symbol, "quantity": result.quantity, "entry_price": candidate.bracket.entry,
                         "stop_price": candidate.bracket.stop, "target_price": candidate.bracket.target,
                         "opened_at": datetime.now(timezone.utc).isoformat(),
                         "entry_order_id": result.broker_order_id, "stop_order_id": None,
                         "strategy": candidate.signal["pattern"], "status": "entry_pending"}
        self.store.save_managed_trade(pending_trade)
        self.store.audit("entry_submitted", {"symbol": symbol, "order_id": result.broker_order_id,
                                             "quantity": result.quantity, "status": result.status})
        info = await self.broker.wait_for_fill(result.broker_order_id)
        state = str(info.get("state", "")).lower()
        if state not in {"filled", "rejected", "failed", "canceled", "cancelled"}:
            await self.broker.cancel_order(result.broker_order_id)
            info = await self.broker.wait_for_fill(result.broker_order_id, timeout_seconds=10)
            state = str(info.get("state", "")).lower()
        filled_qty = float(info.get("cumulative_quantity") or (info.get("quantity") if state == "filled" else 0) or 0)
        if state not in {"filled", "rejected", "failed", "canceled", "cancelled"}:
            log.warning("Entry %s cancellation is not confirmed yet (state=%s); retaining pending state for reconciliation", symbol, state)
            return
        if filled_qty <= 0:
            log.warning("Entry %s had no confirmed fill (state=%s)", symbol, state)
            self.store.update_managed_trade(symbol, status="closed")
            self.store.audit("entry_not_filled", {"symbol": symbol, "order_id": result.broker_order_id, "state": state})
            return
        fill_price = float(info.get("average_price") or info.get("price") or candidate.bracket.entry)
        trade = {**pending_trade, "quantity": filled_qty, "entry_price": fill_price, "status": "stop_submit_pending"}
        self.store.update_managed_trade(symbol, status="stop_submit_pending", quantity=filled_qty, entry_price=fill_price)
        if fill_price <= 0 or filled_qty * fill_price > self.capital_limit:
            log.error("%s fill for %s exceeds the configured %s allocation; requesting immediate reduction", self.settings.broker, symbol, self.capital_unit)
            reduction = await self.broker.submit_order(OrderRequest(
                symbol=symbol, side=Side.SELL, quantity=filled_qty,
                client_order_id=f"cap-exit-{symbol}-{uuid.uuid4().hex[:12]}"))
            self.store.update_managed_trade(symbol, status="emergency_exit_pending", exit_order_id=reduction.broker_order_id)
            self.store.audit("capital_cap_emergency_exit", {"symbol": symbol, "order_id": reduction.broker_order_id,
                                                              "filled_qty": filled_qty, "fill_price": fill_price})
            return
        try:
            stop = await self.broker.place_stop_loss(symbol, filled_qty, candidate.bracket.stop)
            if str(stop.get("state", "")).lower() in {"rejected", "failed", "canceled", "cancelled"}:
                raise RuntimeError(f"protective stop state={stop.get('state')}")
            self.store.update_managed_trade(symbol, status="open", stop_order_id=str(stop["id"]))
        except Exception:
            log.exception("Protective stop rejected for %s; submitting emergency market exit", symbol)
            emergency = await self.broker.submit_order(OrderRequest(
                symbol=symbol, side=Side.SELL, quantity=filled_qty,
                client_order_id=f"no-stop-exit-{symbol}-{uuid.uuid4().hex[:12]}"))
            self.store.update_managed_trade(symbol, status="emergency_exit_pending", exit_order_id=emergency.broker_order_id)
            self.store.audit("protective_stop_rejected_emergency_exit", {"symbol": symbol, "order_id": emergency.broker_order_id})
            return
        trade["stop_order_id"] = str(stop["id"])
        trade["status"] = "open"
        self.store.audit("entry_filled_and_protected", trade)
        log.info("%s position opened and stop submitted: %s qty=%s entry=%.2f stop=%.2f target=%.2f",
                 self.settings.broker, symbol, filled_qty, fill_price, candidate.bracket.stop, candidate.bracket.target)
        await self.notify_telegram(
            f"AUTO ENTRY PROTECTED: {symbol} qty {filled_qty:g} at {self.capital_unit} {fill_price:.2f}; "
            f"stop {candidate.bracket.stop:.2f}, target {candidate.bracket.target:.2f}."
        )

    async def run_cycle(self) -> None:
        now = datetime.now(timezone.utc)
        self.last_cycle_at = now
        local_today = now.astimezone(self.calendar.tz).date()
        is_session_day = bool(self.calendar.is_session(local_today))
        session = self.calendar.date_to_session(
            local_today, direction="none" if is_session_day else "previous")
        session_open = self.calendar.session_open(session).to_pydatetime()
        session_close = self.calendar.session_close(session).to_pydatetime()
        market_open = session_open <= now < session_close
        session_key = str(session.date())
        first_scan = self.last_scan_session != session_key
        interval_elapsed = (self.last_scan_at is None or
                            now - self.last_scan_at >= timedelta(seconds=self.settings.scan_interval_seconds))
        # Run the first quant/history scan immediately when the daemon starts
        # during an open session. Outside market hours, allow one watch-only
        # scan for the most recent session if none has run yet.
        scan_due = (first_scan if not market_open else first_scan or interval_elapsed)
        # During after-hours, do one watch-only scan if today's session was missed.
        # Otherwise avoid account/API polling until the next session.
        if not market_open and not scan_due:
            return

        universe = self.universe()
        risk_warnings: list[str] = []
        positions_known = True
        try:
            positions = await self.broker.get_positions()
            await self.notify_position_changes(positions)
        except Exception:
            positions = []
            positions_known = False
            risk_warnings.append("Could not verify current positions; entries blocked")
            log.exception("Position query failed; quant scan will continue in watch-only mode")
        managed = self.store.managed_trades(status=None)
        quote_symbols = universe + [p.symbol for p in positions if not p.symbol.startswith("CRYPTO:")]
        quote_symbols += [t["symbol"] for t in managed if t["status"] != "closed"]
        try:
            quotes = await self.broker.get_quotes(list(dict.fromkeys(quote_symbols)))
        except Exception:
            quotes = {}
            log.exception("Quote query failed; daily bars can still produce watch-only candidates")
        if market_open and positions_known:
            await self.manage_open_trades(quotes, positions)
        if not scan_due:
            return
        self.last_scan_session = session_key
        self.last_scan_at = now
        self.store.set_runtime_state("last_scan_session", session_key)
        self.store.set_runtime_state("last_scan_at", now.isoformat())

        try:
            risk_warnings.extend(await self.broker.get_risk_warnings())
        except Exception:
            risk_warnings.append("Could not verify all account exposure; entries blocked")
            log.exception("Risk exposure check failed; screening continues in watch-only mode")

        try:
            broker_equity = await self.broker.get_account_equity()
        except Exception:
            broker_equity = self.capital_limit
            risk_warnings.append("Could not verify account equity; entries blocked")
            log.exception("Could not verify %s account equity; screening continues in watch-only mode",
                          self.settings.broker)
        invested = sum(abs(p.market_value) for p in positions)
        positions_by_symbol = {p.symbol: p for p in positions}
        managed_unreflected = sum(
            float(trade["quantity"]) * float(trade["entry_price"])
            for trade in managed if trade["status"] != "closed" and trade["symbol"] not in positions_by_symbol
        )
        try:
            open_buy_commitment = await self.broker.get_open_buy_commitment()
        except Exception:
            open_buy_commitment = 0.0
            risk_warnings.append("Could not value open buy orders; entries blocked")
            log.exception("Open-order exposure check failed; screening continues in watch-only mode")
        available = max(0.0, min(self.capital_limit, broker_equity) - invested
                        - managed_unreflected - open_buy_commitment)
        execution_blockers = list(risk_warnings)
        if self.entries_halted:
            execution_blockers.append("entries halted from Telegram")
        if not market_open:
            execution_blockers.append("market closed; after-hours scan only")
        if not self.settings.live_trading_enabled:
            execution_blockers.append("live order submission disabled")
        if available <= 0:
            execution_blockers.append("no available engine allocation")
        unresolved = [t for t in self.store.managed_trades(status=None)
                      if t["status"] == "quantity_review_required"]
        if unresolved:
            execution_blockers.append("managed position changes require review")
        if risk_warnings:
            self.store.audit("new_entries_blocked_by_unmodeled_exposure", {
                "broker": self.settings.broker, "warnings": risk_warnings,
            })
            log.warning("New entries blocked by account exposure: %s", "; ".join(risk_warnings))
        held_symbols = {p.symbol for p in positions}
        held_symbols.update(t["symbol"] for t in managed if t["status"] != "closed")
        start, end = now - timedelta(days=365 * 5), now
        today_entries = sum(1 for row in self.store.recent_audit(1000)
                            if row["event_type"] == "entry_filled_and_protected"
                            and row["timestamp"].startswith(session_key))
        self.store.audit("scan_started", {"session": session_key, "symbols": len(universe),
                                           "available_capital": available, "capital_unit": self.capital_unit,
                                           "open_buy_commitment": open_buy_commitment,
                                           "live_orders": self.settings.live_trading_enabled,
                                           "market_open": market_open,
                                           "execution_blockers": execution_blockers,
                                           "entries_today": today_entries})
        if today_entries >= self.settings.max_daily_entries:
            log.info("Daily entry limit reached (%d)", today_entries)
            execution_blockers.append("daily entry limit reached")
        history_refreshed = 0
        history_cached = 0
        insufficient_history = 0
        skipped_held = 0
        qualified_candidates = []
        for symbol in universe:
            if symbol in held_symbols:
                skipped_held += 1
                continue
            if available <= 0 and "no available engine allocation" not in execution_blockers:
                execution_blockers.append("no available engine allocation")
            try:
                bars = self.store.get_bars(symbol, "1Day", start, end, self.settings.history_ttl_seconds)
                if bars is None:
                    log.info("Historical cache miss for %s 1Day; fetching broker history", symbol)
                    bars = await self.broker.get_bars(symbol, start, end, "1Day")
                    if bars:
                        self.store.put_bars(symbol, "1Day", bars)
                        history_refreshed += 1
                        self.store.audit("history_refreshed", {"symbol": symbol, "timeframe": "1Day",
                                                               "bars": len(bars), "source": self.settings.broker})
                    log.info("Historical bars fetched for %s: %d", symbol, len(bars or []))
                else:
                    history_cached += 1
                    log.info("Historical cache hit for %s 1Day: %d bars", symbol, len(bars))
                if not bars:
                    continue
                if len(bars) < 210:
                    insufficient_history += 1
                    log.warning("Skipping %s: %d daily bars available; at least 210 are needed for rolling backtest",
                                symbol, len(bars))
                    continue
                price = quotes.get(symbol)
                symbol_blockers = list(execution_blockers)
                if price is None:
                    price = bars[-1].close
                    symbol_blockers.append("live quote unavailable; daily close used for watch only")
                    log.warning("No live quote for %s; using last daily close for watch-only analysis", symbol)
                screen_capital = available if available > 0 and not risk_warnings else self.capital_limit
                bar_payloads = [bar.model_dump() for bar in bars]
                latest = bars[-1]
                signature = (len(bars), latest.timestamp.isoformat(), latest.open,
                             latest.high, latest.low, latest.close, latest.volume)
                cached_backtest = self.backtest_cache.get(symbol)
                if cached_backtest and cached_backtest[0] == signature:
                    backtest = cached_backtest[1]
                else:
                    frame = pd.DataFrame(bar_payloads).sort_values("timestamp")
                    backtest = rolling_backtest(
                        frame, min_trades=self.settings.minimum_backtest_trades,
                        required_win_rate=self.settings.minimum_backtest_win_rate)
                    self.backtest_cache[symbol] = (signature, backtest)
                candidate = screen_symbol(symbol, [bar.model_dump() for bar in bars], risk=self.risk,
                                          available_capital=screen_capital, open_positions=len(positions),
                                          min_trades=self.settings.minimum_backtest_trades,
                                          win_rate=self.settings.minimum_backtest_win_rate,
                                          live_price=price, backtest_result=backtest)
                if not candidate:
                    continue
                qualified_candidates.append(symbol)
                payload = {"symbol": symbol, "pattern": candidate.signal["pattern"],
                           "entry": candidate.bracket.entry, "stop": candidate.bracket.stop,
                           "target": candidate.bracket.target, "quantity": candidate.bracket.quantity,
                           "notional": candidate.bracket.notional, "capital_unit": self.capital_unit,
                           "risk": candidate.bracket.risk_amount,
                           "reward_risk": candidate.bracket.reward_risk,
                           "backtest_trades": candidate.backtest.trades,
                           "backtest_win_rate": candidate.backtest.win_rate}
                self.store.audit("candidate", payload)
                log.info("Qualified %s candidate: %s", self.settings.broker, payload)
                if symbol_blockers:
                    alerts_key = f"watch_alerts_{session_key}"
                    alerted = self.store.get_runtime_state(alerts_key, {})
                    watch_message = (f"WATCH ALERT {symbol}: {candidate.signal['pattern']}\n"
                                     f"Entry {self.capital_unit} {candidate.bracket.entry:.2f}, "
                                     f"stop {candidate.bracket.stop:.2f}, target {candidate.bracket.target:.2f}, "
                                     f"R:R {candidate.bracket.reward_risk:.2f}\n"
                                     f"Backtest: {candidate.backtest.trades} trades, "
                                     f"{candidate.backtest.win_rate:.1%} wins\n"
                                     f"Not submitted: {'; '.join(symbol_blockers)}")
                    if alerted.get(symbol) != candidate.signal["pattern"]:
                        await self.notify_telegram(watch_message)
                        alerted[symbol] = candidate.signal["pattern"]
                        self.store.set_runtime_state(alerts_key, alerted)
                    self.store.audit("watch_alert", {**payload, "blockers": symbol_blockers})
                else:
                    live_blockers = await self._live_entry_blockers(candidate, session, today_entries)
                    if live_blockers:
                        watch_message = (f"WATCH ALERT {symbol}: {candidate.signal['pattern']}\n"
                                         f"Not submitted after live recheck: {'; '.join(live_blockers)}")
                        alerts_key = f"watch_alerts_{session_key}"
                        alerted = self.store.get_runtime_state(alerts_key, {})
                        if alerted.get(symbol) != candidate.signal["pattern"]:
                            await self.notify_telegram(watch_message)
                            alerted[symbol] = candidate.signal["pattern"]
                            self.store.set_runtime_state(alerts_key, alerted)
                        self.store.audit("watch_alert", {**payload, "blockers": live_blockers})
                        continue
                    await self.enter_candidate(candidate)
                    available -= candidate.bracket.notional
                    held_symbols.add(symbol)
                    today_entries += 1
                    if len(held_symbols) >= self.settings.max_open_positions or today_entries >= self.settings.max_daily_entries:
                        break
            except Exception:
                log.exception("Screening/trading failed for %s", symbol)
        self.last_scan_summary = {
            "session": session_key, "symbols": len(universe),
            "history_refreshed": history_refreshed, "history_cached": history_cached,
            "insufficient_history": insufficient_history, "held_symbols_skipped": skipped_held,
            "qualified_candidates": qualified_candidates,
            "execution_blockers": execution_blockers,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.store.set_runtime_state("last_scan_summary", self.last_scan_summary)
        self.store.audit("scan_completed", self.last_scan_summary)
        log.info("Quant scan complete: %d symbols, refreshed=%d cached=%d insufficient_history=%d candidates=%s blockers=%s",
                 len(universe), history_refreshed, history_cached, insufficient_history,
                 qualified_candidates or "none", execution_blockers or "none")
        no_setup_key = f"no_setup_notice_{session_key}"
        if not qualified_candidates and not self.store.get_runtime_state(no_setup_key, False):
            await self.notify_telegram(
                f"Daily quant scan {session_key}: no setup passed the pattern/backtest filters. "
                f"eligible symbols: {len(universe) - skipped_held}; history refreshed: {history_refreshed}, cache hits: {history_cached}, "
                f"under 210 bars: {insufficient_history}."
            )
            self.store.set_runtime_state(no_setup_key, True)

    async def _live_entry_blockers(self, candidate, session, today_entries: int) -> list[str]:
        blockers = []
        if not self.settings.live_trading_enabled:
            blockers.append("live entries disabled")
        if self.entries_halted:
            blockers.append("entries halted")
        if not self.is_open(datetime.now(timezone.utc), session):
            blockers.append("market closed before submission")
        if today_entries >= self.settings.max_daily_entries:
            blockers.append("daily entry limit reached")
        try:
            positions = await self.broker.get_positions()
            current_symbols = {position.symbol for position in positions}
            active_trades = [trade for trade in self.store.managed_trades(status=None)
                             if trade["status"] != "closed"]
            managed_symbols = {trade["symbol"] for trade in active_trades}
            if candidate.symbol in current_symbols or candidate.symbol in managed_symbols:
                blockers.append("symbol is already held at broker")
            if len(current_symbols | managed_symbols) >= self.settings.max_open_positions:
                blockers.append("maximum open positions reached")
            warnings = await self.broker.get_risk_warnings()
            blockers.extend(warnings)
            equity = await self.broker.get_account_equity()
            commitment = await self.broker.get_open_buy_commitment()
            invested = sum(abs(position.market_value) for position in positions)
            reflected = {position.symbol for position in positions}
            unreflected = sum(float(trade["quantity"]) * float(trade["entry_price"])
                              for trade in active_trades if trade["symbol"] not in reflected)
            available = max(0.0, min(self.capital_limit, equity) - invested - commitment - unreflected)
            if candidate.bracket.notional > available:
                blockers.append("current available allocation is below candidate notional")
        except Exception:
            log.exception("Final live risk recheck failed for %s", candidate.symbol)
            blockers.append("broker risk recheck failed")
        return blockers

    async def create_review_report(self) -> str:
        positions = await self.broker.get_positions()
        by_symbol = {p.symbol: p for p in positions}
        managed = self.store.managed_trades(status=None)
        managed_symbols = {trade["symbol"] for trade in managed if trade["status"] != "closed"}
        discrepancies: list[str] = []

        for trade in managed:
            if trade["status"] == "closed":
                continue
            position = by_symbol.get(trade["symbol"])
            if position is None and trade["status"] in {"open", "quantity_review_required", "manual_close_cancel_pending"}:
                if trade["status"] != "manual_close_cancel_pending":
                    self.store.update_managed_trade(trade["symbol"], status="manual_close_cancel_pending")
                    trade["status"] = "manual_close_cancel_pending"
                await self._finish_pending_exit(trade)
                discrepancies.append(f"{trade['symbol']}: absent at broker; protective stop cancellation requested")
            elif position is not None:
                actual_quantity = abs(float(position.quantity))
                expected_quantity = float(trade["quantity"])
                if abs(actual_quantity - expected_quantity) > 1e-6:
                    if trade["status"] != "quantity_review_required":
                        self.store.update_managed_trade(trade["symbol"], status="quantity_review_required")
                        self.set_entries_halted(True)
                        self.store.audit("managed_quantity_changed_externally", {
                            "symbol": trade["symbol"], "managed_quantity": expected_quantity,
                            "broker_quantity": actual_quantity,
                        })
                    discrepancies.append(
                        f"{trade['symbol']}: broker qty {actual_quantity:g}, plan qty {expected_quantity:g}; review required"
                    )
                elif trade["status"] == "quantity_review_required":
                    self.store.update_managed_trade(trade["symbol"], status="open")

        unmanaged = [p for p in positions if not p.symbol.startswith("CRYPTO:")
                     and p.symbol not in managed_symbols]
        manual_quant_checks = []
        if unmanaged:
            discrepancies.extend(f"{p.symbol}: unmanaged broker position qty {p.quantity:g}"
                                 for p in unmanaged[:15])
            for position in unmanaged[:5]:
                manual_quant_checks.append(await self._review_manual_position(position))

        try:
            warnings = await self.broker.get_risk_warnings()
        except Exception:
            warnings = ["Could not verify all broker exposure"]
        known_deployed = sum(abs(p.market_value) for p in positions)
        equity = None
        try:
            equity = await self.broker.get_account_equity()
            commitment = await self.broker.get_open_buy_commitment()
            allocation = max(0.0, min(self.capital_limit, equity) - known_deployed - commitment)
            capital_line = (f"Equity {self.capital_unit} {equity:,.2f}; known positions "
                            f"{self.capital_unit} {known_deployed:,.2f}; known available "
                            f"{self.capital_unit} {allocation:,.2f}")
        except Exception:
            capital_line = "Account allocation unavailable; no new entries should be placed"
        if warnings:
            if equity is None:
                capital_line = "Account allocation unknown due to unmodeled broker exposure"
            else:
                capital_line = (f"Equity {self.capital_unit} {equity:,.2f}; known positions "
                                f"{self.capital_unit} {known_deployed:,.2f}; allocation unknown due to unmodeled exposure")

        summary = self.last_scan_summary or self.store.get_runtime_state("last_scan_summary", {})
        if summary:
            scan_line = (f"Scan {summary.get('session')}: {summary.get('symbols', 0)} symbols, "
                         f"{summary.get('history_refreshed', 0)} history refreshes, "
                         f"{summary.get('history_cached', 0)} cache hits, "
                         f"candidates {', '.join(summary.get('qualified_candidates', [])) or 'none'}")
        else:
            scan_line = "No completed quant scan is recorded yet"
        if warnings:
            discrepancies.extend(f"Risk guard: {warning}" for warning in warnings)
        review_day = datetime.now(self.calendar.tz).date().isoformat()
        for event in self.store.recent_audit(100):
            if not event["timestamp"].startswith(review_day):
                continue
            details = event["details"]
            if event["event_type"] == "external_position_change":
                discrepancies.extend(f"Manual broker activity: {change}" for change in details.get("changes", []))
            elif event["event_type"] == "managed_position_missing":
                discrepancies.append(f"{details.get('symbol')}: managed position disappeared; stop cancellation reconciled")
        state_line = "New entries HALTED" if self.entries_halted else "New entries enabled"
        if discrepancies:
            discrepancy_line = "\n".join(f"- {item}" for item in discrepancies[:15])
        else:
            discrepancy_line = "Broker positions match the managed trade ledger"
        holdings_line = ", ".join(f"{p.symbol} {p.quantity:g}" for p in positions[:15]) or "none"
        quant_line = "\nManual position quant checks:\n" + "\n".join(manual_quant_checks) if manual_quant_checks else ""
        return (f"6 PM trading review | {self.settings.broker}\n{state_line}\n{capital_line}\n"
                f"Broker positions: {holdings_line}\n{scan_line}{quant_line}\nReconciliation:\n{discrepancy_line}\n"
                "Manual order changes are reported for review; unmanaged positions are not auto-adopted.")

    async def _review_manual_position(self, position) -> str:
        symbol = position.symbol
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=365 * 5)
            bars = self.store.get_bars(symbol, "1Day", start, end, self.settings.history_ttl_seconds)
            if bars is None:
                bars = await self.broker.get_bars(symbol, start, end, "1Day")
                if bars:
                    self.store.put_bars(symbol, "1Day", bars)
                    self.store.audit("history_refreshed", {"symbol": symbol, "timeframe": "1Day",
                                                           "bars": len(bars), "source": self.settings.broker,
                                                           "purpose": "manual_position_review"})
            if len(bars or []) < 210:
                return f"{symbol}: only {len(bars or [])} daily bars; at least 210 needed for backtest"
            payload = [bar.model_dump() for bar in bars]
            frame = pd.DataFrame(payload).sort_values("timestamp")
            backtest = rolling_backtest(frame, min_trades=self.settings.minimum_backtest_trades,
                                        required_win_rate=self.settings.minimum_backtest_win_rate)
            price = abs(position.market_value / position.quantity) if position.quantity else None
            candidate = screen_symbol(
                symbol, payload, risk=self.risk, available_capital=self.capital_limit,
                open_positions=0, min_trades=self.settings.minimum_backtest_trades,
                win_rate=self.settings.minimum_backtest_win_rate, live_price=price,
                backtest_result=backtest)
            if candidate:
                return (f"{symbol}: QUALIFIED {candidate.signal['pattern']}; backtest "
                        f"{backtest.trades} trades / {backtest.win_rate:.1%}; research bracket "
                        f"{candidate.bracket.entry:.2f}/{candidate.bracket.stop:.2f}/{candidate.bracket.target:.2f}")
            signal = evaluate_setup(frame, symbol)
            reason = (f"backtest below threshold ({backtest.trades} trades / {backtest.win_rate:.1%})"
                      if not backtest.qualified else f"no buy setup ({signal.get('pattern', 'none')})")
            return f"{symbol}: not qualified by current quant plan; {reason}"
        except Exception:
            log.exception("Manual position quant review failed for %s", symbol)
            return f"{symbol}: quant review unavailable; see daemon logs"

    async def maybe_send_evening_review(self) -> None:
        if self.telegram_app is None:
            return
        if not self.store.get_runtime_state("telegram_chat_started", False):
            return
        local_now = datetime.now(self.calendar.tz)
        if local_now.time() < clock_time(18, 0) or not self.calendar.is_session(local_now.date()):
            return
        review_date = local_now.date().isoformat()
        if self.store.get_runtime_state("last_evening_review_date") == review_date:
            return
        last_attempt = self.store.get_runtime_state("last_evening_review_attempt")
        if last_attempt and local_now - datetime.fromisoformat(last_attempt) < timedelta(minutes=15):
            return
        self.store.set_runtime_state("last_evening_review_attempt", local_now.isoformat())
        report = await self.create_review_report()
        if await self.notify_telegram(report):
            self.store.set_runtime_state("last_evening_review_date", review_date)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        log.info("daemon started broker=%s live_orders_enabled=%s scan_interval=%ss telegram_configured=%s",
                 self.settings.broker, self.settings.live_trading_enabled,
                 self.settings.scan_interval_seconds,
                 bool(self.settings.telegram_bot_token and self.settings.telegram_authorized_user_id))
        telegram_app = None
        try:
            from trading_engine.telegram_control import build_application
            telegram_app = build_application(self, self.settings)
            if telegram_app is not None:
                await telegram_app.initialize()
                await telegram_app.start()
                await telegram_app.updater.start_polling(drop_pending_updates=True)
                self.telegram_app = telegram_app
                log.info("Telegram remote control started")
            while not self.stop.is_set():
                try:
                    await self.run_cycle()
                except Exception:
                    log.exception("cycle failed")
                try:
                    await self.maybe_send_evening_review()
                except Exception:
                    log.exception("evening review failed")
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.settings.poll_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            if telegram_app is not None:
                if telegram_app.updater and telegram_app.updater.running:
                    await telegram_app.updater.stop()
                if telegram_app.running:
                    await telegram_app.stop()
                await telegram_app.shutdown()
            self.telegram_app = None
            await self.broker.close()
            fcntl.flock(self._instance_lock.fileno(), fcntl.LOCK_UN)
            self._instance_lock.close()
            log.info("daemon stopped")


def main() -> None:
    asyncio.run(TradingDaemon().run())


if __name__ == "__main__":
    main()
