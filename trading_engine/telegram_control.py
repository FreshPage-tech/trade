from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, filters

from trading_engine.config.settings import Settings

log = logging.getLogger("trading_engine.telegram")


def build_application(daemon, settings: Settings) -> Application | None:
    """Build a private-user-only remote control for the running daemon."""
    if not settings.telegram_bot_token or not settings.telegram_authorized_user_id:
        log.info("Telegram remote control is disabled (no bot token and authorized user configured)")
        return None

    token = settings.telegram_bot_token.get_secret_value()
    authorized = filters.User(user_id=settings.telegram_authorized_user_id) & filters.ChatType.PRIVATE
    application = ApplicationBuilder().token(token).build()

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        daemon.store.set_runtime_state("telegram_chat_started", True)
        await update.effective_message.reply_text(
            "Trading engine remote control is connected. Send /help for commands. "
            "Keep this private chat open to receive the 6 PM review."
        )

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            equity = await daemon.broker.get_account_equity()
            positions = await daemon.broker.get_positions()
            commitment = await daemon.broker.get_open_buy_commitment()
            warnings = await daemon.broker.get_risk_warnings()
            unit = daemon.capital_unit
            deployed = sum(abs(p.market_value) for p in positions)
            allocation = None if warnings else max(
                0.0, min(daemon.capital_limit, equity) - deployed - commitment)
            if settings.broker == "alpaca" and settings.alpaca_paper:
                account_mode = "Alpaca paper account"
            elif settings.broker == "paper":
                account_mode = "local simulation"
            else:
                account_mode = "live account"
            entries = "HALTED" if daemon.entries_halted else "enabled"
            if not settings.live_trading_enabled:
                entries += " (live entry submission disabled)"
            cycle = daemon.last_cycle_at.isoformat(timespec="seconds") if daemon.last_cycle_at else "starting"
            allocation_text = f"{unit} {allocation:,.2f}" if allocation is not None else "not safely calculable"
            risk_text = ("Risk guard: new entries blocked\n" + "\n".join(f"- {item}" for item in warnings)
                         if warnings else "Risk guard: clear for new entries")
            await update.effective_message.reply_text(
                f"{settings.broker.upper()} | {account_mode}\n"
                f"New entries: {entries}\n"
                f"Equity: {unit} {equity:,.2f}\n"
                f"Engine cap: {unit} {daemon.capital_limit:,.2f}\n"
                f"Position value: {unit} {deployed:,.2f}\n"
                f"Open buy commitment: {unit} {commitment:,.2f}\n"
                f"Available allocation: {allocation_text}\n"
                f"{risk_text}\n"
                + f"\nOpen positions: {len(positions)}\nLast cycle: {cycle}"
            )
        except Exception:
            log.exception("Telegram status request failed")
            await update.effective_message.reply_text("Status unavailable. Check the daemon logs.")

    async def positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            current = await daemon.broker.get_positions()
            if not current:
                warnings = await daemon.broker.get_risk_warnings()
                suffix = "\n" + "\n".join(warnings) if warnings else ""
                await update.effective_message.reply_text("No valued stock/crypto positions." + suffix)
                return
            unit = daemon.capital_unit
            lines = [f"{p.symbol}: qty={p.quantity:g}, avg={unit} {p.average_entry_price:,.2f}, "
                     f"value={unit} {p.market_value:,.2f}" for p in current]
            warnings = await daemon.broker.get_risk_warnings()
            suffix = "\nRisk warnings: " + "; ".join(warnings) if warnings else ""
            await update.effective_message.reply_text("Open positions\n" + "\n".join(lines) + suffix)
        except Exception:
            log.exception("Telegram positions request failed")
            await update.effective_message.reply_text("Positions unavailable. Check the daemon logs.")

    async def halt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        daemon.set_entries_halted(True)
        await update.effective_message.reply_text(
            "New strategy entries halted. Existing positions continue to be monitored and protected."
        )

    async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        unresolved = [trade for trade in daemon.store.managed_trades(status=None)
                      if trade["status"] == "quantity_review_required"]
        if unresolved:
            symbols = ", ".join(trade["symbol"] for trade in unresolved)
            await update.effective_message.reply_text(
                f"Cannot resume: reconcile managed quantities in Robinhood first ({symbols})."
            )
            return
        daemon.set_entries_halted(False)
        if settings.live_trading_enabled:
            message = "New strategy entries resumed. The configured broker account may receive live orders."
        else:
            message = "Scanning resumed, but live entry submission is disabled in the environment configuration."
        await update.effective_message.reply_text(message)

    async def review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await update.effective_message.reply_text(await daemon.create_review_report())
        except Exception:
            log.exception("Telegram evening review failed")
            await update.effective_message.reply_text("Review unavailable. Check the daemon logs.")

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            "Trading engine remote control\n"
            "/status - account, allocation, daemon heartbeat\n"
            "/positions - broker positions\n"
            "/review - compare broker positions with the managed plan and scan history\n"
            "/halt - stop new strategy entries (position management continues)\n"
            "/resume - resume strategy entries\n"
            "/help - show commands"
        )

    for name, handler in (("start", start), ("status", status), ("positions", positions),
                          ("review", review), ("halt", halt), ("resume", resume),
                          ("help", help_command)):
        application.add_handler(CommandHandler(name, handler, filters=authorized))

    async def report_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Telegram update failed", exc_info=context.error)

    application.add_error_handler(report_error)
    return application
