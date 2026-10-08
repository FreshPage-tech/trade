from __future__ import annotations

import json

from trading_engine.brokers.alpaca_broker import AlpacaBroker
from trading_engine.brokers.paper_broker import PaperBroker
from trading_engine.brokers.robinhood_broker import RobinhoodBroker
from trading_engine.brokers.zerodha_broker import ZerodhaBroker
from trading_engine.core.rate_limiter import AsyncTokenBucket


def build_broker(settings):
    limiter = AsyncTokenBucket(settings.request_rate_per_second, settings.request_burst)
    if settings.broker == "paper":
        return PaperBroker(settings.capital_limit_usd)
    if settings.broker == "robinhood":
        if not settings.robinhood_username or not settings.robinhood_password:
            raise RuntimeError("Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD in trading_engine/.env")
        return RobinhoodBroker(settings.robinhood_username.get_secret_value(),
                               settings.robinhood_password.get_secret_value(), limiter=limiter,
                               mfa_code=settings.robinhood_mfa_code.get_secret_value() if settings.robinhood_mfa_code else None,
                               totp_secret=settings.robinhood_totp_secret.get_secret_value() if settings.robinhood_totp_secret else None)
    if settings.broker == "alpaca":
        if not settings.alpaca_api_key or not settings.alpaca_secret_key:
            raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in trading_engine/.env")
        return AlpacaBroker(settings.alpaca_api_key.get_secret_value(),
                            settings.alpaca_secret_key.get_secret_value(),
                            paper=settings.alpaca_paper, limiter=limiter)
    if settings.broker == "zerodha":
        if not settings.kite_api_key or not settings.kite_access_token:
            raise RuntimeError("Set KITE_API_KEY and the current KITE_ACCESS_TOKEN in trading_engine/.env")
        map_path = settings.kite_instrument_map
        if not map_path.exists():
            raise RuntimeError(f"Kite instrument map not found: {map_path}")
        with map_path.open(encoding="utf-8") as handle:
            raw_map = json.load(handle)
        instrument_map = {symbol: (item["exchange"], int(item["instrument_token"]))
                          for symbol, item in raw_map.items()}
        if not instrument_map:
            raise RuntimeError("Add Kite exchange and instrument_token entries to config/kite_instruments.json")
        return ZerodhaBroker(settings.kite_api_key.get_secret_value(),
                             settings.kite_access_token.get_secret_value(), instrument_map,
                             limiter=limiter)
    raise ValueError(f"Unsupported broker: {settings.broker}")
