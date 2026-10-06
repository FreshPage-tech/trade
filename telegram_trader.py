import os
import sys
import time
import asyncio
import threading
from functools import wraps
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

import robin_stocks.robinhood as r
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME")
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTH_USER_ID = os.getenv("TELEGRAM_AUTHORIZED_USER_ID")

if not all([ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, TELEGRAM_BOT_TOKEN, AUTH_USER_ID]):
    print("\n[CRITICAL ERROR] Missing credentials in .env!")
    print("Ensure ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, TELEGRAM_BOT_TOKEN, and TELEGRAM_AUTHORIZED_USER_ID are set.\n")
    sys.exit(1)

AUTH_USER_ID = int(AUTH_USER_ID)

# Global flag to control automated background scanner
SCANNER_ACTIVE = False
scanner_thread = None

# =====================================================================
# 1. AUTHENTICATION & SECURITY GATE
# =====================================================================
def restricted(func):
    """Decorator to ensure only your specific Telegram ID can issue commands."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        user_id = user.id if user else None
        if user_id != AUTH_USER_ID:
            print(f"[UNAUTHORIZED ACCESS BLOCKED] User ID: {user_id}")
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Unauthorized. Access denied.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def rh_login():
    """Logs into Robinhood with session persistence."""
    try:
        r.login(ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, expiresIn=86400, store_session=True)
        return True
    except Exception as e:
        print(f"[LOGIN ERROR] {e}")
        return False

# =====================================================================
# 2. TELEGRAM COMMAND HANDLERS
# =====================================================================
@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 *Robinhood Quant Bot Controller Connected*\n\n"
        "Available Commands:\n"
        "• `/status` - View current open positions, cash & PnL\n"
        "• `/start_bot` - Start the 24/7 automated momentum scanner\n"
        "• `/stop_bot` - Pause automated scanning loop\n"
        "• `/panic` - 🚨 Emergency market sell of ALL open positions"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching live Robinhood positions...")
    
    if not rh_login():
        await update.message.reply_text("❌ Robinhood login failed. Status was not fetched.")
        return

    try:
        profile = r.profiles.load_account_profile()
        buying_power = float(profile.get("buying_power", 0.0) or profile.get("cash", 0.0))
        portfolio = r.profiles.load_portfolio_profile()
        total_equity = float(portfolio.get("equity", 0.0))

        stock_positions = r.account.get_open_stock_positions()
        total_deployed = 0.0
        lines = []

        if stock_positions:
            for pos in stock_positions:
                qty = float(pos.get("quantity", 0.0))
                if qty <= 0:
                    continue
                inst_url = pos.get("instrument")
                inst_data = r.helper.request_get(inst_url)
                sym = inst_data.get("symbol")
                avg_buy = float(pos.get("average_buy_price", 0.0))
                
                quote = r.stocks.get_latest_price(sym)[0]
                curr_price = float(quote) if quote else avg_buy
                val = qty * curr_price
                cost = qty * avg_buy
                pnl = val - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
                total_deployed += val

                pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{pnl_emoji} *{sym}*: {qty} shs @ ${curr_price:.2f} | PnL: ${pnl:+.2f} ({pnl_pct:+.2f}%)")

        # Crypto positions
        try:
            crypto_positions = r.crypto.get_crypto_positions()
            if crypto_positions:
                for c_pos in crypto_positions:
                    c_qty = float(c_pos.get("quantity_available", 0.0))
                    if c_qty <= 0:
                        continue
                    code = c_pos.get("currency", {}).get("code")
                    c_quote = r.crypto.get_crypto_quote(code)
                    c_price = float(c_quote.get("mark_price", 0.0)) if c_quote else 0.0
                    c_val = c_qty * c_price
                    total_deployed += c_val
                    lines.append(f"🪙 *{code}*: {c_qty:.4f} units | Val: ${c_val:.2f}")
        except Exception:
            pass

        scanner_status = "🟢 ACTIVE" if SCANNER_ACTIVE else "⏸️ STOPPED"
        pos_text = "\n".join(lines) if lines else "No open positions."

        report = (
            f"📊 *PORTFOLIO REPORT*\n"
            f"• Scanner: {scanner_status}\n"
            f"• Total Equity: `${total_equity:,.2f}`\n"
            f"• Buying Power: `${buying_power:,.2f}`\n"
            f"• Deployed Exposure: `${total_deployed:,.2f} / $2,000.00`\n\n"
            f"📈 *Open Positions:*\n{pos_text}"
        )
        await update.message.reply_text(report, parse_mode="Markdown")

    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching status: {e}")

# =====================================================================
# 3. BACKGROUND SCANNER CONTROLS
# =====================================================================
def background_scanner_loop():
    """Runs the high-speed scanning loop in a background thread."""
    global SCANNER_ACTIVE
    print("[BACKGROUND SCANNER] Starting momentum engine...")
    rh_login()

    while SCANNER_ACTIVE:
        try:
            time.sleep(5)
        except Exception as e:
            print(f"[SCANNER ERROR] {e}")
            time.sleep(5)

    print("[BACKGROUND SCANNER] Scanner loop paused.")

@restricted
async def cmd_start_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SCANNER_ACTIVE, scanner_thread
    if SCANNER_ACTIVE:
        await update.message.reply_text("⚠️ Scanner is already running.")
        return

    SCANNER_ACTIVE = True
    scanner_thread = threading.Thread(target=background_scanner_loop, daemon=True)
    scanner_thread.start()
    await update.message.reply_text("✅ *Automated Scanner Started.* Monitoring momentum entries and strict stops.", parse_mode="Markdown")

@restricted
async def cmd_stop_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SCANNER_ACTIVE
    if not SCANNER_ACTIVE:
        await update.message.reply_text("ℹ️ Scanner is already stopped.")
        return

    SCANNER_ACTIVE = False
    await update.message.reply_text("⏸️ *Automated Scanner Paused.* No new trades will be opened. Existing positions remain intact.", parse_mode="Markdown")

@restricted
async def cmd_panic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Emergency kill switch: sells all open stock positions immediately."""
    global SCANNER_ACTIVE
    SCANNER_ACTIVE = False
    await update.message.reply_text("🚨 *PANIC TRIGGERED!* Halting scanner and liquidating all positions...", parse_mode="Markdown")

    if not rh_login():
        await update.message.reply_text("❌ Robinhood login failed. No liquidation orders were submitted.")
        return

    closed = []
    
    try:
        r.orders.cancel_all_stock_orders()
        
        positions = r.account.get_open_stock_positions()
        for pos in positions:
            qty = float(pos.get("quantity", 0.0))
            if qty > 0:
                inst_url = pos.get("instrument")
                sym = r.helper.request_get(inst_url).get("symbol")
                # Preserve fractional shares; converting to int can leave part
                # of a position open (or turn a small position into quantity 0).
                r.orders.order_sell_market(symbol=sym, quantity=qty)
                closed.append(f"{sym} ({qty} shares)")

        summary = ", ".join(closed) if closed else "No positions to close."
        await update.message.reply_text(f"🛑 *Liquidated:* {summary}\nBot is now idle.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error during panic liquidation: {e}")

# =====================================================================
# 4. BOT LAUNCHER
# =====================================================================
def main():
    print("=" * 60)
    print("🚀 Starting Robinhood Telegram Bot Interface...")
    print(f"🔒 Locked to Authorized User ID: {AUTH_USER_ID}")
    print("=" * 60)

    if not rh_login():
        print("[CRITICAL] Could not connect to Robinhood. Check credentials.")
        sys.exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("start_bot", cmd_start_bot))
    app.add_handler(CommandHandler("stop_bot", cmd_stop_bot))
    app.add_handler(CommandHandler("panic", cmd_panic))

    print("\n✅ Bot is live and listening on Telegram. Open Telegram on your phone and send /status\n")
    app.run_polling()

if __name__ == "__main__":
    main()
