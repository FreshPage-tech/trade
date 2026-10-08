from __future__ import annotations

from dataclasses import dataclass
import logging

import pandas as pd

from trading_engine.core.risk_manager import Bracket, RiskManager, RiskRejected
from trading_engine.strategies.backtester import BacktestResult, rolling_backtest
from trading_engine.trade import evaluate_setup

log = logging.getLogger("trading_engine.screener")


@dataclass(frozen=True)
class Candidate:
    symbol: str
    signal: dict
    bracket: Bracket
    backtest: BacktestResult


def screen_symbol(symbol: str, bars: list[dict] | pd.DataFrame, *, risk: RiskManager,
                  available_capital: float, open_positions: int,
                  min_trades: int = 30, win_rate: float = 0.75,
                  live_price: float | None = None,
                  backtest_result: BacktestResult | None = None) -> Candidate | None:
    frame = bars.copy() if isinstance(bars, pd.DataFrame) else pd.DataFrame(bars)
    if frame.empty:
        return None
    frame = frame.sort_values("timestamp") if "timestamp" in frame else frame
    # Normalize broker candle keys to the shared strategy OHLCV schema.
    frame = frame.rename(columns={"open_price": "open", "high_price": "high",
                                  "low_price": "low", "close_price": "close"})
    bt = backtest_result or rolling_backtest(frame, min_trades=min_trades, required_win_rate=win_rate)
    if not bt.qualified:
        log.info("Backtest rejected %s: trades=%d win_rate=%.1f%% required=%d / %.1f%%",
                 symbol, bt.trades, bt.win_rate * 100, min_trades, win_rate * 100)
        return None
    signal_frame = frame.copy()
    if live_price is not None and live_price > 0:
        last_index = signal_frame.index[-1]
        signal_frame.loc[last_index, "close"] = live_price
        signal_frame.loc[last_index, "high"] = max(float(signal_frame.loc[last_index, "high"]), live_price)
        signal_frame.loc[last_index, "low"] = min(float(signal_frame.loc[last_index, "low"]), live_price)
    signal = evaluate_setup(signal_frame, symbol, current_inventory=open_positions)
    if signal.get("action") != "BUY":
        log.info("Quant/pattern screen rejected %s after qualified backtest", symbol)
        return None
    entry = float(live_price or signal["price"])
    original_entry = float(signal["price"])
    stop = float(signal["stop_loss"])
    target = float(signal["take_profit"])
    per_share_risk = entry - stop
    original_risk = original_entry - stop
    if per_share_risk <= 0 or original_risk <= 0:
        return None
    rr = (target - original_entry) / original_risk
    adjusted_target = entry + rr * per_share_risk
    try:
        bracket = risk.bracket(entry, stop, adjusted_target,
                               available_capital=available_capital,
                               open_positions=open_positions)
    except RiskRejected:
        return None
    return Candidate(symbol, signal, bracket, bt)
