"""
telegram_trader.py: Master 24/7 Trading Agent with Complete Telegram Command Suite
"""

import os
import sys
import time
import json
import csv
import logging
import psutil
import subprocess
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Set, Tuple

import pandas as pd
import matplotlib
matplotlib.use('Agg')
import mplfinance as mpf
from dotenv import load_dotenv
import robin_stocks.robinhood as r
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from trade import evaluate_setup, compute_indicators, detect_chart_patterns

# =====================================================================
# 1. CONFIGURATION & STATE MANAGEMENT
# =====================================================================
BASE_DIR = Path("/home/ubuntu/RobinhoodBot")
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME")
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD")

# Dynamic Settings (Modifiable via Telegram commands)
CONFIG = {
    "TOTAL_PORTFOLIO_CAP": 2500.00,
    "SHARES_PER_TRADE": 1,
    "MAX_PRICE_LIMIT": 50.00,
    "MAX_TRADES_PER_DAY": 10,
    "CHECK_INTERVAL_SECONDS": 30,
    "PAPER_TRADING": False,
    "TRADING_HALTED": False
}

LOG_FILE_PATH       = BASE_DIR / "trade_performance.csv"
RESEARCH_LOG_PATH   = BASE_DIR / "trade_research_sheet.csv"
ACTIVE_TRADES_JSON  = BASE_DIR / "active_trades.json"
BLACKLIST_FILE_PATH = BASE_DIR / "invalid_tickers.txt"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MasterTrader")

# =====================================================================
# 2. LOGGING & DATA HELPERS
# =====================================================================
def init_csv_logs():
    if not LOG_FILE_PATH.exists():
        with open(LOG_FILE_PATH, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Timestamp", "Ticker", "Action", "Quantity",
                "Entry_Price", "Stop_Loss", "Target_TP", "Exit_Price",
                "Gross_PnL", "Return_Pct", "Outcome", "Exit_Reason", "ETA_Label", "Pattern"
            ])
    if not RESEARCH_LOG_PATH.exists():
        with open(RESEARCH_LOG_PATH, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Timestamp", "Ticker", "Price", "Pattern", "Accuracy", "ETA", "SL", "TP", "Decision"])

def load_active_trades() -> Dict[str, dict]:
    if ACTIVE_TRADES_JSON.exists():
        try:
            with open(ACTIVE_TRADES_JSON, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_active_trades(trades: dict):
    with open(ACTIVE_TRADES_JSON, 'w') as f:
        json.dump(trades, f, indent=4)

def log_trade_close(trade: dict, exit_price: float, reason: str):
    entry_p = float(trade['entry_price'])
    shares = float(trade['shares'])
    pnl = (exit_price - entry_p) * shares
    ret_pct = ((exit_price - entry_p) / entry_p) * 100.0
    outcome = "WIN" if pnl > 0 else "LOSS"

    with open(LOG_FILE_PATH, mode='a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade['symbol'], "SELL", shares,
            f"{entry_p:.2f}", f"{trade['stop_loss']:.2f}", f"{trade['take_profit']:.2f}", f"{exit_price:.2f}",
            f"{pnl:.2f}", f"{ret_pct:.2f}%", outcome, reason, trade.get('eta_label', 'N/A'), trade.get('pattern', 'N/A')
        ])

# =====================================================================
# 3. AGENT CORE ORCHESTRATION
# =====================================================================
class RobinhoodMasterAgent:
    def __init__(self):
        init_csv_logs()
        self.invalid_tickers: Set[str] = self.load_blacklist()
        self.active_universe: List[str] = []
        self.rolling_bars: Dict[str, List[dict]] = {}
        self.daily_trades = 0
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.login()
        self.load_universe()

    def login(self):
        try:
            r.login(username=ROBINHOOD_USERNAME, password=ROBINHOOD_PASSWORD, expiresIn=86400*30, store_session=True)
            logger.info("Robinhood session active.")
        except Exception as e:
            logger.error(f"Robinhood login error: {e}")

    def load_blacklist(self) -> Set[str]:
        bad = {"GPS", "WBA", "SAVE", "BITF"}
        if BLACKLIST_FILE_PATH.exists():
            try:
                with open(BLACKLIST_FILE_PATH, 'r') as f:
                    bad.update(line.strip().upper() for line in f if line.strip())
            except Exception:
                pass
        return bad

    def save_blacklist(self):
        with open(BLACKLIST_FILE_PATH, 'w') as f:
            for s in sorted(self.invalid_tickers):
                f.write(f"{s}\n")

    def load_universe(self):
        defaults = [
            "SOFI", "HOOD", "AFRM", "NU", "BAC", "BBD", "VALE", "OPEN", "CLOV",
            "PLTR", "SNAP", "DKNG", "PINS", "PATH", "GRAB", "WBD", "KVUE", "SOUN", "BBAI",
            "INTC", "CSCO", "KMI", "BMY", "PFE", "NOK", "ERIC", "LUMN",
            "F", "RIVN", "LCID", "NIO", "RUN", "PLUG", "TLRY", "BLNK", "CHPT",
            "ASTS", "RKLB", "JOBY", "ACHR", "AUR", "PLRX", "DNA",
            "MARA", "RIOT", "CLSK", "HUT", "CIFR", "AAL", "CCL", "NCLH", "JBLU"
        ]
        self.active_universe = [t for t in defaults if t not in self.invalid_tickers]

    def get_invested_capital(self) -> float:
        trades = load_active_trades()
        return sum(float(t['shares']) * float(t['entry_price']) for t in trades.values())

    def update_history(self, symbol: str, quote: dict):
        if symbol not in self.rolling_bars:
            self.rolling_bars[symbol] = []
        p = float(quote['last_trade_price'])
        prev = float(quote.get('previous_close', p))
        self.rolling_bars[symbol].append({
            'open': prev,
            'high': max(p, prev),
            'low': min(p, prev),
            'close': p,
            'volume': 0
        })
        if len(self.rolling_bars[symbol]) > 50:
            self.rolling_bars[symbol] = self.rolling_bars[symbol][-50:]

agent = RobinhoodMasterAgent()

# =====================================================================
# 4. BACKGROUND RECURRING ENGINE LOOP
# =====================================================================
async def scanner_and_manager_loop(context: ContextTypes.DEFAULT_TYPE):
    if CONFIG["TRADING_HALTED"]:
        return

    trades = load_active_trades()
    now_str = datetime.now().strftime("%Y-%m-%d")
    if now_str != agent.today:
        agent.today = now_str
        agent.daily_trades = 0

    symbols_to_query = list(set(agent.active_universe + list(trades.keys())))
    quotes = {}
    try:
        raw_quotes = r.get_quotes(symbols_to_query)
        for i, q in enumerate(raw_quotes):
            if q and q.get('last_trade_price'):
                sym = symbols_to_query[i]
                quotes[sym] = q
                agent.update_history(sym, q)
    except Exception as e:
        logger.error(f"Quote fetch error: {e}")
        return

    # Check active trades for TP, SL, and ETA time decay
    closed_tickers = []
    for sym, trade in trades.items():
        if sym not in quotes:
            continue

        curr_p = float(quotes[sym]['last_trade_price'])
        entry_p = float(trade['entry_price'])
        sl = float(trade['stop_loss'])
        tp = float(trade['take_profit'])
        eta_h = float(trade.get('eta_hours', 4.0))

        entry_dt = datetime.fromisoformat(trade['entry_time'])
        hrs_elapsed = (datetime.now() - entry_dt).total_seconds() / 3600.0
        pnl_pct = ((curr_p - entry_p) / entry_p) * 100.0

        exit_reason = None
        if curr_p >= tp:
            exit_reason = f"🎯 TARGET REACHED (+{pnl_pct:.2f}%)"
        elif curr_p <= sl:
            exit_reason = f"🛑 STOP LOSS HIT ({pnl_pct:.2f}%)"
        elif hrs_elapsed >= eta_h and pnl_pct >= -0.30:
            exit_reason = f"⏱️ ETA EXPIRED ({trade.get('eta_label')}). Flat momentum ({pnl_pct:+.2f}%)."

        if exit_reason:
            if not CONFIG["PAPER_TRADING"]:
                r.orders.order_sell_market(symbol=sym, quantity=int(trade['shares']))
            log_trade_close(trade, curr_p, exit_reason)
            closed_tickers.append(sym)

            msg = (
                f"🔔 *TRADE CLOSED: {sym}*\n"
                f"• Reason: {exit_reason}\n"
                f"• Entry: ${entry_p:.2f} | Exit:${curr_p:.2f}\n"
                f"• Duration: {hrs_elapsed:.1f}h (Target was {trade.get('eta_label')})\n"
                f"• Pattern: {trade.get('pattern', 'N/A')}"
            )
            if TELEGRAM_CHAT_ID:
                await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

    for t in closed_tickers:
        del trades[t]
    if closed_tickers:
        save_active_trades(trades)

    # Automated Universe Scan for Confluence Entries
    invested = agent.get_invested_capital()
    if agent.daily_trades >= CONFIG["MAX_TRADES_PER_DAY"] or invested >= CONFIG["TOTAL_PORTFOLIO_CAP"]:
        return

    for sym in agent.active_universe:
        if sym in trades or sym not in quotes:
            continue

        history = agent.rolling_bars.get(sym, [])
        if len(history) < 20:
            continue

        df = pd.DataFrame(history)
        curr_p = float(quotes[sym]['last_trade_price'])
        if curr_p > CONFIG["MAX_PRICE_LIMIT"] or (invested + curr_p) > CONFIG["TOTAL_PORTFOLIO_CAP"]:
            continue

        signal = evaluate_setup(df, sym, current_inventory=len(trades))
        if signal["action"] == "BUY":
            if not CONFIG["PAPER_TRADING"]:
                res = r.orders.order_buy_market(symbol=sym, quantity=CONFIG["SHARES_PER_TRADE"])
                if not res or "id" not in res:
                    continue

            trades[sym] = {
                "symbol": sym,
                "shares": CONFIG["SHARES_PER_TRADE"],
                "entry_price": curr_p,
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "pattern": signal["pattern"],
                "accuracy": signal["accuracy"],
                "eta_hours": signal["eta_hours"],
                "eta_label": signal["eta_label"],
                "entry_time": datetime.now().isoformat()
            }
            save_active_trades(trades)
            agent.daily_trades += 1
            invested = agent.get_invested_capital()

            msg = (
                f"🚀 *NEW TRADE ENTERED: {sym}*\n"
                f"• Price: ${curr_p:.2f} ({CONFIG['SHARES_PER_TRADE']} share)\n"
                f"• Pattern: *{signal['pattern']}* ({signal['accuracy']} accuracy)\n"
                f"• ETA: *{signal['eta_label']}*\n"
                f"• TP: ${signal['take_profit']:.2f} | SL:${signal['stop_loss']:.2f}\n"
                f"• Reason: {signal['reason']}\n"
                f"• Deployed: ${invested:.2f} /${CONFIG['TOTAL_PORTFOLIO_CAP']:.2f}"
            )
            if TELEGRAM_CHAT_ID:
                await context.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

            if invested >= CONFIG["TOTAL_PORTFOLIO_CAP"] or agent.daily_trades >= CONFIG["MAX_TRADES_PER_DAY"]:
                break

# =====================================================================
# 5. ALL 22 TELEGRAM COMMAND IMPLEMENTATIONS
# =====================================================================

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = load_active_trades()
    invested = agent.get_invested_capital()
    total_trades, wins = 0, 0
    if LOG_FILE_PATH.exists():
        with open(LOG_FILE_PATH, 'r') as f:
            for r_row in csv.DictReader(f):
                if r_row.get("Outcome") in ["WIN", "LOSS"]:
                    total_trades += 1
                    if r_row.get("Outcome") == "WIN":
                        wins += 1
    win_rate = (wins / total_trades * 100.0) if total_trades > 0 else 100.0
    mode_str = "📝 PAPER" if CONFIG["PAPER_TRADING"] else "💰 LIVE REAL-MONEY"
    state_str = "⏸️ HALTED" if CONFIG["TRADING_HALTED"] else "▶️ ACTIVE"

    msg = (
        f"📊 *PORTFOLIO & SYSTEM STATUS*\n\n"
        f"• Mode: *{mode_str}* ({state_str})\n"
        f"• Deployed Capital: *${invested:.2f} /${CONFIG['TOTAL_PORTFOLIO_CAP']:.2f}*\n"
        f"• Open Positions: *{len(trades)}*\n"
        f"• Today's Executions: *{agent.daily_trades} / {CONFIG['MAX_TRADES_PER_DAY']}*\n"
        f"• Historical Win Rate: *{win_rate:.1f}%* ({wins}W / {total_trades}T)\n"
        f"• Scanned Universe: *{len(agent.active_universe)} tickers*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trades = load_active_trades()
    if not trades:
        await update.message.reply_text("ℹ️ No active positions currently open.")
        return

    msg = "📋 *Active Positions & ETA Tracking:*\n\n"
    for sym, t in trades.items():
        q = r.get_quotes(sym)
        curr = float(q[0]['last_trade_price']) if q and q[0] else float(t['entry_price'])
        pnl = ((curr - float(t['entry_price'])) / float(t['entry_price'])) * 100.0
        hrs = (datetime.now() - datetime.fromisoformat(t['entry_time'])).total_seconds() / 3600.0

        msg += (
            f"• *{sym}*: ${curr:.2f} ({pnl:+.2f}%)\n"
            f"  Pattern: {t.get('pattern')} | ETA: {t.get('eta_label')} (Held {hrs:.1f}h)\n"
            f"  Target TP: ${t['take_profit']:.2f} \vert{} Stop Loss:${t['stop_loss']:.2f}\n\n"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/close <SYMBOL>`")
        return
    sym = context.args[0].upper()
    trades = load_active_trades()
    if sym not in trades:
        await update.message.reply_text(f"❌ {sym} is not in active positions.")
        return

    trade = trades[sym]
    q = r.get_quotes(sym)
    curr_p = float(q[0]['last_trade_price']) if q and q[0] else float(trade['entry_price'])
    if not CONFIG["PAPER_TRADING"]:
        r.orders.order_sell_market(symbol=sym, quantity=int(trade['shares']))

    log_trade_close(trade, curr_p, "MANUAL_CLOSE")
    del trades[sym]
    save_active_trades(trades)
    await update.message.reply_text(f"✅ Closed {sym} @ ${curr_p:.2f}. Capital released.")

async def cmd_panic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CONFIG["TRADING_HALTED"] = True
    trades = load_active_trades()
    count = len(trades)
    for sym, trade in trades.items():
        try:
            if not CONFIG["PAPER_TRADING"]:
                r.orders.order_sell_market(symbol=sym, quantity=int(trade['shares']))
            q = r.get_quotes(sym)
            curr = float(q[0]['last_trade_price']) if q and q[0] else float(trade['entry_price'])
            log_trade_close(trade, curr, "EMERGENCY_PANIC_LIQUIDATION")
        except Exception as e:
            logger.error(f"Panic close failed on {sym}: {e}")

    trades.clear()
    save_active_trades(trades)
    await update.message.reply_text(f"🚨 *PANIC EXECUTED*: Liquidated {count} positions. Trading is now **HALTED**.\nUse `/mode live` to resume.", parse_mode="Markdown")

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/buy <SYMBOL> [SHARES]`")
        return
    sym = context.args[0].upper()
    qty = int(context.args[1]) if len(context.args) > 1 else 1

    try:
        q = r.get_quotes(sym)
        if not q or not q[0] or not q[0].get('last_trade_price'):
            await update.message.reply_text(f"❌ Could not find market price for {sym}.")
            return
        price = float(q[0]['last_trade_price'])

        if not CONFIG["PAPER_TRADING"]:
            res = r.orders.order_buy_market(symbol=sym, quantity=qty)
            if not res or "id" not in res:
                await update.message.reply_text(f"❌ Order rejected: {res}")
                return

        trades = load_active_trades()
        trades[sym] = {
            "symbol": sym,
            "shares": qty,
            "entry_price": price,
            "stop_loss": round(price * 0.97, 2),
            "take_profit": round(price * 1.05, 2),
            "pattern": "Manual Telegram Buy",
            "accuracy": "N/A",
            "eta_hours": 8.0,
            "eta_label": "8 Hours",
            "entry_time": datetime.now().isoformat()
        }
        save_active_trades(trades)
        await update.message.reply_text(f"✅ Bought {qty}x {sym} @ ${price:.2f}. Auto-monitoring attached.")
    except Exception as e:
        await update.message.reply_text(f"❌ Execution error: {e}")

async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/sell <SYMBOL> [SHARES]`")
        return
    sym = context.args[0].upper()
    qty = int(context.args[1]) if len(context.args) > 1 else 1

    try:
        if not CONFIG["PAPER_TRADING"]:
            r.orders.order_sell_market(symbol=sym, quantity=qty)
        trades = load_active_trades()
        if sym in trades:
            del trades[sym]
            save_active_trades(trades)
        await update.message.reply_text(f"✅ Market sell order sent for {qty}x {sym}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Sell error: {e}")

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/scan <SYMBOL>`")
        return
    sym = context.args[0].upper()
    await update.message.reply_text(f"🔍 Analyzing {sym}...")

    try:
        # Fetch 60 daily intervals
        historicals = r.stocks.get_stock_historicals(sym, interval='day', span='3month')
        if not historicals or len(historicals) < 20:
            await update.message.reply_text(f"⚠️ Insufficient historical data to scan {sym}.")
            return

        df = pd.DataFrame([{
            'close': float(h['close_price']),
            'high': float(h['high_price']),
            'low': float(h['low_price']),
            'open': float(h['open_price']),
            'volume': int(h['volume'])
        } for h in historicals])

        df = compute_indicators(df)
        pattern = detect_chart_patterns(df)
        latest = df.iloc[-1]
        trend = "Bullish (Above 200 EMA)" if latest['close'] > latest['EMA_200'] else "Bearish (Below 200 EMA)"

        msg = (
            f"🔎 *Technical Analysis: {sym}*\n"
            f"• Price: *${latest['close']:.2f}*\n"
            f"• RSI (14): *{latest['RSI']:.1f}*\n"
            f"• ATR (14): *${latest['ATR']:.2f}*\n"
            f"• Macro Trend: *{trend}*\n"
            f"• Pattern: *{pattern['pattern'] if pattern else 'No Pattern Detected'}*\n"
            f"• Historic Accuracy: *{pattern['accuracy'] if pattern else 'N/A'}*\n"
            f"• Recommended Action: *{'BUY' if pattern and latest['close'] > latest['EMA_200'] else 'HOLD / WAIT'}*"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Scan error: {e}")

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/chart <SYMBOL>`")
        return
    sym = context.args[0].upper()
    await update.message.reply_text(f"🎨 Generating technical chart for {sym}...")

    try:
        historicals = r.stocks.get_stock_historicals(sym, interval='day', span='3month')
        if not historicals:
            await update.message.reply_text(f"❌ Could not retrieve chart data for {sym}.")
            return

        df = pd.DataFrame([{
            'Date': pd.to_datetime(h['begins_at']),
            'Open': float(h['open_price']),
            'High': float(h['high_price']),
            'Low': float(h['low_price']),
            'Close': float(h['close_price']),
            'Volume': int(h['volume'])
        } for h in historicals]).set_index('Date')

        buf = BytesIO()
        mpf.plot(
            df,
            type='candle',
            mav=(20, 50),
            volume=True,
            style='yahoo',
            savefig=dict(fname=buf, dpi=120, bbox_inches='tight')
        )
        buf.seek(0)
        await update.message.reply_photo(photo=buf, caption=f"📈 Daily Candlestick with 20/50 MAV: {sym}")
    except Exception as e:
        await update.message.reply_text(f"❌ Chart generation error: {e}")

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    syms = ", ".join(agent.active_universe[:30])
    msg = f"📋 *Active Watchlist Universe ({len(agent.active_universe)} tickers):*\n`{syms}`"
    if len(agent.active_universe) > 30:
        msg += f"\n_...and {len(agent.active_universe) - 30} more._"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/add <SYMBOL>`")
        return
    sym = context.args[0].upper()
    if sym in agent.active_universe:
        await update.message.reply_text(f"ℹ️ {sym} is already in the watchlist.")
        return
    agent.active_universe.append(sym)
    if sym in agent.invalid_tickers:
        agent.invalid_tickers.remove(sym)
        agent.save_blacklist()
    await update.message.reply_text(f"✅ Added *{sym}* to scanning universe.", parse_mode="Markdown")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/remove <SYMBOL>`")
        return
    sym = context.args[0].upper()
    if sym in agent.active_universe:
        agent.active_universe.remove(sym)
        await update.message.reply_text(f"🗑️ Removed *{sym}* from watchlist.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ {sym} is not in current watchlist.")

async def cmd_blacklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/blacklist <SYMBOL>`")
        return
    sym = context.args[0].upper()
    agent.invalid_tickers.add(sym)
    agent.save_blacklist()
    if sym in agent.active_universe:
        agent.active_universe.remove(sym)
    await update.message.reply_text(f"🚫 *{sym}* permanently added to blacklist.", parse_mode="Markdown")

async def cmd_set_sl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/set_sl <SYMBOL> <PRICE>`")
        return
    sym, val = context.args[0].upper(), float(context.args[1])
    trades = load_active_trades()
    if sym in trades:
        trades[sym]['stop_loss'] = val
        save_active_trades(trades)
        await update.message.reply_text(f"✅ Updated Stop Loss for {sym} to **${val:.2f}**", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No active position found for {sym}.")

async def cmd_set_tp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/set_tp <SYMBOL> <PRICE>`")
        return
    sym, val = context.args[0].upper(), float(context.args[1])
    trades = load_active_trades()
    if sym in trades:
        trades[sym]['take_profit'] = val
        save_active_trades(trades)
        await update.message.reply_text(f"✅ Updated Take Profit for {sym} to **${val:.2f}**", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No active position found for {sym}.")

async def cmd_set_eta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/set_eta <SYMBOL> <HOURS>`")
        return
    sym, hrs = context.args[0].upper(), float(context.args[1])
    trades = load_active_trades()
    if sym in trades:
        trades[sym]['eta_hours'] = hrs
        trades[sym]['eta_label'] = f"{hrs} Hours"
        save_active_trades(trades)
        await update.message.reply_text(f"✅ Updated holding ETA for {sym} to **{hrs} hours**.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No active position found for {sym}.")

async def cmd_set_cap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/set_cap <DOLLARS>`")
        return
    cap = float(context.args[0])
    CONFIG["TOTAL_PORTFOLIO_CAP"] = cap
    await update.message.reply_text(f"✅ Max portfolio exposure cap set to **${cap:.2f}**", parse_mode="Markdown")

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"Current mode: {'PAPER' if CONFIG['PAPER_TRADING'] else 'LIVE'}\nUsage: `/mode <live|paper>`")
        return
    target = context.args[0].lower()
    if target == "live":
        CONFIG["PAPER_TRADING"] = False
        CONFIG["TRADING_HALTED"] = False
        await update.message.reply_text("🟢 Mode switched to **LIVE REAL-MONEY EXECUTION**.", parse_mode="Markdown")
    elif target == "paper":
        CONFIG["PAPER_TRADING"] = True
        CONFIG["TRADING_HALTED"] = False
        await update.message.reply_text("📝 Mode switched to **PAPER SIMULATION**.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Invalid option. Use `/mode live` or `/mode paper`.")

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LOG_FILE_PATH.exists():
        await update.message.reply_text("No trade performance logs found.")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    week_ago = datetime.now() - timedelta(days=7)

    today_pnl, week_pnl, total_pnl = 0.0, 0.0, 0.0
    wins, total = 0, 0

    with open(LOG_FILE_PATH, 'r') as f:
        for r_row in csv.DictReader(f):
            try:
                pnl = float(r_row.get("Gross_PnL", 0.0))
                ts_dt = datetime.strptime(r_row["Timestamp"], "%Y-%m-%d %H:%M:%S")
                total_pnl += pnl
                total += 1
                if pnl > 0:
                    wins += 1
                if r_row["Timestamp"].startswith(today_str):
                    today_pnl += pnl
                if ts_dt >= week_ago:
                    week_pnl += pnl
            except Exception:
                continue

    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    msg = (
        f"💵 *PROFIT & LOSS BREAKDOWN*\n\n"
        f"• Today's PnL: *${today_pnl:+.2f}*\n"
        f"• Past 7 Days: *${week_pnl:+.2f}*\n"
        f"• Lifetime Realized PnL: *${total_pnl:+.2f}*\n"
        f"• Total Trades: *{total}* (Win Rate: *{win_rate:.1f}%*)"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if LOG_FILE_PATH.exists():
        with open(LOG_FILE_PATH, 'rb') as f:
            await update.message.reply_document(document=f, filename="trade_performance.csv")
    if RESEARCH_LOG_PATH.exists():
        with open(RESEARCH_LOG_PATH, 'rb') as f:
            await update.message.reply_document(document=f, filename="trade_research_sheet.csv")

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = subprocess.run(["journalctl", "-u", "tradingbot", "-n", "25", "--no-pager"], capture_output=True, text=True)
        text = res.stdout[-3800:] if len(res.stdout) > 3800 else res.stdout
        await update.message.reply_text(f"```text\n{text}\n```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error fetching logs: {e}")

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Pulling latest code and restarting service...")
    try:
        subprocess.run(["git", "pull", "origin", "main"], cwd=str(BASE_DIR), capture_output=True)
        subprocess.run(["sudo", "systemctl", "restart", "tradingbot"])
        await update.message.reply_text("✅ Service restarted successfully.")
    except Exception as e:
        await update.message.reply_text(f"❌ Reload failed: {e}")

async def cmd_system(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    uptime_seconds = time.time() - psutil.boot_time()
    uptime_hours = uptime_seconds / 3600.0

    msg = (
        f"🖥️ *ORACLE VM SYSTEM METRICS*\n\n"
        f"• CPU Usage: *{cpu}%*\n"
        f"• RAM Usage: *{ram.percent}%* ({ram.used // (1024**2)}MB / {ram.total // (1024**2)}MB)\n"
        f"• Disk Space: *{disk.percent}%* ({disk.free // (1024**3)}GB Free)\n"
        f"• VM Uptime: *{uptime_hours:.1f} hours*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# =====================================================================
# 6. MAIN APPLICATION BOOTSTRAP
# =====================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("[ERROR] Missing TELEGRAM_BOT_TOKEN in .env")
        sys.exit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Core Monitoring
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("position", cmd_positions))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("system", cmd_system))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("export_csv", cmd_export_csv))

    # Manual Execution
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("panic", cmd_panic))

    # Research & Charting
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("chart", cmd_chart))

    # Universe & Watchlist
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))

    # Configuration & Tuning
    app.add_handler(CommandHandler("set_sl", cmd_set_sl))
    app.add_handler(CommandHandler("set_tp", cmd_set_tp))
    app.add_handler(CommandHandler("set_eta", cmd_set_eta))
    app.add_handler(CommandHandler("set_cap", cmd_set_cap))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("reload", cmd_reload))

    # Background Scanner & ETA Manager
    app.job_queue.run_repeating(scanner_and_manager_loop, interval=CONFIG["CHECK_INTERVAL_SECONDS"], first=5)

    print("🚀 Master Telegram Interface Live. Ready for commands.")
    app.run_polling()

if __name__ == "__main__":
    main()