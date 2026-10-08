from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from trading_engine.brokers.factory import build_broker
from trading_engine.config.settings import Settings, get_settings


BROKER_ALIASES = {
    "robinhood": "robinhood", "rh": "robinhood",
    "alpaca": "alpaca",
    "kite": "zerodha", "zerodha": "zerodha",
}
DISPLAY_NAMES = {"zerodha": "Kite / Zerodha", "robinhood": "Robinhood",
                 "alpaca": "Alpaca", "paper": "Paper"}


def _selected_settings(broker_name: str) -> Settings:
    canonical = BROKER_ALIASES[broker_name.lower()]
    configured = get_settings()
    data = configured.model_dump()
    data["broker"] = canonical

    # Keep Robinhood's existing DB path for continuity; isolate alternate broker
    # state so identical ticker symbols cannot share orders or managed positions.
    db_path: Path = configured.database_path
    if canonical != "robinhood":
        suffix = db_path.suffix or ".sqlite3"
        data["database_path"] = db_path.with_name(f"{db_path.stem}_{canonical}{suffix}")
    return Settings.model_validate(data)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Run or inspect a configured trading broker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    choices = sorted(BROKER_ALIASES)
    run_parser = subparsers.add_parser("run", help="start the event-driven trading daemon")
    run_parser.add_argument("broker", choices=choices)
    status_parser = subparsers.add_parser("status", help="read account and position status")
    status_parser.add_argument("broker", choices=choices)
    return parser


async def _status(settings: Settings) -> None:
    broker = build_broker(settings)
    unit = "INR" if settings.broker == "zerodha" else "USD"
    cap = settings.kite_capital_limit_inr if settings.broker == "zerodha" else settings.capital_limit_usd
    try:
        warnings = []
        try:
            equity = await broker.get_account_equity()
        except Exception:
            equity = None
            warnings.append("Account equity could not be loaded")
        try:
            positions = await broker.get_positions()
        except Exception:
            positions = None
            warnings.append("Positions could not be fully valued")
        try:
            commitment = await broker.get_open_buy_commitment()
        except Exception:
            commitment = None
            warnings.append("Open buy commitment could not be valued")
        try:
            warnings.extend(await broker.get_risk_warnings())
        except Exception:
            warnings.append("Additional broker exposure could not be checked")

        deployed = sum(abs(position.market_value) for position in positions or [])
        allocation = None
        if not warnings and equity is not None and positions is not None and commitment is not None:
            allocation = max(0.0, min(cap, equity) - deployed - commitment)
        if settings.broker == "paper":
            mode = "LOCAL SIMULATION"
        elif settings.broker == "alpaca" and settings.alpaca_paper:
            mode = "ALPACA PAPER ACCOUNT" + (" (orders enabled)" if settings.live_trading_enabled else " (entries disabled)")
        else:
            mode = "LIVE ORDERS ENABLED" if settings.live_trading_enabled else "LIVE ACCOUNT / ENTRIES DISABLED"
        print(f"Broker: {DISPLAY_NAMES[settings.broker]} | {mode}")
        print(f"Account equity:       {unit} {equity:,.2f}" if equity is not None
              else "Account equity:       unavailable")
        print(f"Engine capital cap:   {unit} {cap:,.2f}")
        print(f"Open position value:  {unit} {deployed:,.2f}" if positions is not None
              else "Open position value:  unavailable")
        print(f"Open buy commitment:  {unit} {commitment:,.2f}" if commitment is not None
              else "Open buy commitment:  unavailable")
        print(f"Available allocation: {unit} {allocation:,.2f}" if allocation is not None
              else "Available allocation: not safely calculable")
        if warnings:
            print("Risk/status warnings:")
            for warning in warnings:
                print(f"  - {warning}")
        if positions:
            print("\nPositions:")
            for position in positions:
                print(f"  {position.symbol:<12} qty={position.quantity:g}  "
                      f"avg={unit} {position.average_entry_price:,.2f}  "
                      f"value={unit} {position.market_value:,.2f}")
        elif positions is not None:
            print("\nPositions: none")
        else:
            print("\nPositions: unavailable")
    finally:
        await broker.close()


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = _selected_settings(args.broker)
    if args.command == "run":
        from trading_engine.main import TradingDaemon

        if settings.live_trading_enabled:
            account_mode = "paper" if settings.broker == "paper" or (
                settings.broker == "alpaca" and settings.alpaca_paper) else "live"
            logging.getLogger("trading_engine").warning(
                "%s order submission is enabled (account_mode=%s)", settings.broker, account_mode)
        asyncio.run(TradingDaemon(settings=settings).run())
    else:
        asyncio.run(_status(settings))
    return 0
