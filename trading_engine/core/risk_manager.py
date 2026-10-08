from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bracket:
    entry: float
    stop: float
    target: float
    quantity: int
    risk_amount: float
    notional: float
    reward_risk: float


class RiskRejected(ValueError):
    pass


class RiskManager:
    """Currency-agnostic notional cap plus per-trade loss budget."""

    def __init__(self, capital_limit: float = 2000.0, risk_fraction: float = 0.005,
                 max_positions: int = 5):
        if capital_limit <= 0:
            raise ValueError("capital_limit must be positive")
        if not 0 < risk_fraction <= 0.02:
            raise ValueError("risk_fraction must be in (0, 2%]")
        self.capital_limit = capital_limit
        self.risk_fraction = risk_fraction
        self.max_positions = max_positions

    def bracket(self, entry: float, stop: float, target: float, *,
                available_capital: float, open_positions: int,
                reward_risk_min: float = 2.0, reward_risk_max: float = 4.0) -> Bracket:
        if min(entry, stop, target, available_capital) <= 0 or stop >= entry or target <= entry:
            raise RiskRejected("invalid long bracket or capital")
        if open_positions >= self.max_positions:
            raise RiskRejected("maximum open positions reached")
        per_share_risk = entry - stop
        rr = (target - entry) / per_share_risk
        if not reward_risk_min <= rr <= reward_risk_max:
            raise RiskRejected(f"reward/risk {rr:.2f} outside [{reward_risk_min}, {reward_risk_max}]")
        risk_budget = min(self.capital_limit * self.risk_fraction, available_capital * self.risk_fraction)
        quantity = int(min(risk_budget / per_share_risk,
                           self.capital_limit / entry,
                           available_capital / entry))
        if quantity < 1:
            raise RiskRejected("risk or capital budget cannot fund one whole share")
        notional = quantity * entry
        if notional > self.capital_limit or notional > available_capital:
            raise RiskRejected("hard capital bound exceeded")
        return Bracket(entry, stop, target, quantity, quantity * per_share_risk,
                       notional, rr)
