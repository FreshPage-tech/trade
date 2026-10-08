from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PatternSignal:
    name: str
    entry: float
    stop: float
    invalidation: str


def detect_patterns(frame: pd.DataFrame, lookback: int = 20) -> list[PatternSignal]:
    """Simple, rule-based candidate detection; outputs are hypotheses, not win rates."""
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns) or len(frame) < max(lookback + 1, 22):
        return []
    f = frame.tail(lookback + 1).copy()
    prior, latest = f.iloc[:-1], f.iloc[-1]
    atr = (prior.high - prior.low).rolling(14).mean().iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return []
    out = []
    resistance = float(prior.high.max())
    support = float(prior.low.min())
    if latest.close > resistance and latest.volume >= prior.volume.tail(10).median():
        out.append(PatternSignal("range_breakout", float(latest.close), max(support, float(latest.close - atr)), "close below breakout range"))
    # Sweep and reclaim: trades below recent support intrabar, then closes back above.
    if latest.low < support and latest.close > support and latest.close > latest.open:
        out.append(PatternSignal("liquidity_sweep_reclaim", float(latest.close), float(latest.low - 0.1 * atr), "close below sweep low"))
    # Support test: recent lows cluster within 0.5 ATR and latest candle closes bullish.
    lows = prior.low.tail(8)
    if len(lows) == 8 and float(lows.max() - lows.min()) <= 0.5 * atr and latest.close > latest.open and latest.close > float(lows.median()):
        out.append(PatternSignal("support_reclaim", float(latest.close), float(lows.min() - 0.25 * atr), "close below support cluster"))
    return out
