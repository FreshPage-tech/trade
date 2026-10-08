from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Bar(BaseModel):
    model_config = ConfigDict(frozen=True)
    symbol: str
    timestamp: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(default=None, gt=0)
    client_order_id: str


class OrderResult(BaseModel):
    broker_order_id: str
    client_order_id: str
    status: str
    symbol: str
    quantity: float
    side: Side
    submitted_at: datetime
    average_fill_price: float | None = None
    raw_status: str | None = None


class Position(BaseModel):
    symbol: str
    quantity: float
    average_entry_price: float
    market_value: float


class BaseBroker(ABC):
    """Broker boundary. Implementations must document order-status semantics."""

    name: str

    @abstractmethod
    async def get_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str = "1Day") -> list[Bar]:
        raise NotImplementedError

    async def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Batch quote fallback from recent bars; adapters should override where possible."""
        result: dict[str, float] = {}
        from datetime import timedelta, timezone
        end = datetime.now(timezone.utc)
        for symbol in symbols:
            bars = await self.get_bars(symbol, end - timedelta(days=7), end, "1Day")
            if bars:
                result[symbol] = bars[-1].close
        return result

    @abstractmethod
    async def submit_order(self, order: OrderRequest) -> OrderResult:
        raise NotImplementedError

    async def get_open_buy_commitment(self) -> float:
        """Notional reserved by unfilled buy orders, in the broker account currency."""
        return 0.0

    async def get_risk_warnings(self) -> list[str]:
        """Exposure that prevents safe new entries but should not hide account status."""
        return []

    async def order_info(self, order_id: str) -> dict:
        raise NotImplementedError

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20) -> dict:
        raise NotImplementedError

    async def place_stop_loss(self, symbol: str, quantity: float, stop_price: float) -> dict:
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> dict:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        raise NotImplementedError

    @abstractmethod
    async def get_account_equity(self) -> float:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release network/session resources, if any."""
        return None
