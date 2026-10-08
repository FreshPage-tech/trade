from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from trading_engine.trade import evaluate_setup


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    wins: int
    losses: int
    win_rate: float
    average_return_r: float
    max_drawdown_r: float
    qualified: bool


def rolling_backtest(frame: pd.DataFrame, *, min_trades: int = 30,
                     required_win_rate: float = 0.75, holding_bars: int = 10) -> BacktestResult:
    """Chronological walk-forward simulation of the same pattern/confluence signals.

    Entries are at the next bar open. If stop and target are both touched in one bar,
    the stop is counted first. Fees, spread and slippage are excluded, so results are
    optimistic and must not be read as a live performance estimate.
    """
    returns: list[float] = []
    if len(frame) < 210:
        return BacktestResult(0, 0, 0, 0.0, 0.0, 0.0, False)
    i = 200
    while i < len(frame) - 1:
        signal = evaluate_setup(frame.iloc[:i + 1], "BACKTEST")
        if signal.get("action") != "BUY":
            i += 1
            continue
        entry_i = i + 1
        entry = float(frame.iloc[entry_i].open)
        stop = float(signal.get("stop_loss") or 0)
        target = float(signal.get("take_profit") or 0)
        risk = entry - stop
        if risk <= 0 or target <= entry:
            i += 1
            continue
        end_i = min(len(frame), entry_i + holding_bars)
        result_r = None
        exit_i = end_i - 1
        for j in range(entry_i, end_i):
            bar = frame.iloc[j]
            if float(bar.low) <= stop:
                result_r, exit_i = -1.0, j
                break
            if float(bar.high) >= target:
                result_r, exit_i = (target - entry) / risk, j
                break
        if result_r is None:
            result_r = (float(frame.iloc[end_i - 1].close) - entry) / risk
        returns.append(result_r)
        i = exit_i + 1

    wins = sum(value > 0 for value in returns)
    win_rate = wins / len(returns) if returns else 0.0
    equity = peak = max_dd = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return BacktestResult(len(returns), wins, len(returns) - wins, win_rate,
                          sum(returns) / len(returns) if returns else 0.0,
                          max_dd, len(returns) >= min_trades and win_rate >= required_win_rate)
