import os
import sys
import time
import math
import csv
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Set
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import robin_stocks.robinhood as r
from dotenv import load_dotenv

# =====================================================================
# 1. ENVIRONMENT & CONFIGURATION
# =====================================================================
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)

ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME")
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD")

if not all([ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD]):
    print("\n[CRITICAL ERROR] Missing Robinhood credentials in .env!")
    print(f"Please ensure {ENV_PATH} contains ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD.\n")
    sys.exit(1)

# --- PORTFOLIO & STRATEGY CONSTRAINTS ---
TOTAL_PORTFOLIO_CAP    = 200.00            # HARD CEILING: Max $200 total open exposure
SHARES_PER_TRADE       = 1                 # 1 share per stock setup
MAX_PRICE_LIMIT        = 50.00             # Hard price ceiling per individual stock
MAX_TRADES_PER_DAY     = 6                 # Maximum round-trip trades allowed per day
RISK_REWARD_RATIO      = 2.5               # 2.5x Reward vs Risk target
CHECK_INTERVAL_SECONDS = 5                 # Polling interval between scans

# --- WIN RATE GATEKEEPER ---
MIN_REQUIRED_WIN_RATE  = 75.0              # 75% minimum win rate requirement
MIN_SAMPLE_SIZE_TRADES = 5                 # Trades required before lock engages

# --- OPERATIONAL MODE ---
PAPER_TRADING          = False             # Set to False for LIVE real-money execution
LOG_FILE_PATH          = BASE_DIR / "trade_performance.csv"
EXCEL_WATCHLIST_PATH   = BASE_DIR / "stocks_universe.xlsx"
CSV_WATCHLIST_PATH     = BASE_DIR / "stocks_universe.csv"
BLACKLIST_FILE_PATH    = BASE_DIR / "invalid_tickers.txt"

# =====================================================================
# 2. DATA STRUCTURES & MOMENTUM ENGINE
# =====================================================================
@dataclass
class MarketData:
    symbol: str
    high: float
    low: float
    close: float
    open: float
    volume: int = 0

@dataclass
class ActivePosition:
    symbol: str
    shares: int
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_timestamp: str

class HighSpeedMomentumEngine:
    """Maintains rolling ATR and RSI histories across all candidate stocks."""
    def __init__(self, atr_periods: int = 14, rsi_periods: int = 14):
        self.atr_periods = atr_periods
        self.rsi_periods = rsi_periods
        self.histories: Dict[str, List[MarketData]] = {}

    def update_ticker(self, candle: MarketData):
        sym = candle.symbol
        if sym not in self.histories:
            self.histories[sym] = []
        self.histories[sym].append(candle)
        if len(self.histories[sym]) > 60:
            self.histories[sym] = self.histories[sym][-60:]

    def purge_ticker(self, sym: str):
        if sym in self.histories:
            del self.histories[sym]

    def calculate_atr(self, sym: str) -> float:
        history = self.histories.get(sym, [])
        if len(history) < self.atr_periods + 1:
            return 0.0

        true_ranges = []
        for i in range(1, len(history)):
            curr = history[i]
            prev = history[i - 1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close)
            )
            true_ranges.append(tr)
        return float(np.mean(true_ranges[-self.atr_periods:]))

    def calculate_rsi(self, sym: str) -> float:
        history = self.histories.get(sym, [])
        if len(history) < self.rsi_periods + 1:
            return 50.0

        closes = [c.close for c in history]
        deltas = np.diff(closes[-(self.rsi_periods + 1):])
        gains = deltas[deltas > 0]
        losses = -deltas[deltas < 0]

        avg_gain = float(np.mean(gains)) if len(gains) > 0 else 0.0
        avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0.0

        if avg_loss == 0.0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def evaluate_breakout(self, sym: str) -> Tuple[bool, float, float, float]:
        history = self.histories.get(sym, [])
        if len(history) < 15:
            return False, 0.0, 0.0, 0.0

        current = history[-1]
        prior_bars = history[-11:-1]
        highest_prior_high = max(c.high for c in prior_bars)

        atr = self.calculate_atr(sym)
        rsi = self.calculate_rsi(sym)

        price_breakout = current.close > highest_prior_high
        momentum_filter = 50.0 <= rsi <= 68.0

        if price_breakout and momentum_filter and atr > 0.0:
            entry_price = current.close
            stop_distance = 1.5 * atr
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + (stop_distance * RISK_REWARD_RATIO)
            return True, entry_price, stop_loss, take_profit

        return False, 0.0, 0.0, 0.0

# =====================================================================
# 3. PERFORMANCE & AUDITING
# =====================================================================
class PerformanceAuditor:
    def __init__(self, filepath: Path):
        self.filepath = filepath
        if not self.filepath.exists():
            with open(self.filepath, mode='w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([
                    "Entry_Timestamp", "Exit_Timestamp", "Symbol", "Shares",
                    "Entry_Price", "Exit_Price", "Stop_Loss", "Take_Profit",
                    "Gross_PnL", "Return_Pct", "Outcome", "Exit_Reason", "Mode"
                ])

    def get_realized_win_rate(self) -> Tuple[float, int]:
        if not self.filepath.exists():
            return 100.0, 0

        wins = 0
        total = 0
        with open(self.filepath, mode='r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                if row.get("Outcome") in ["WIN", "LOSS"]:
                    total += 1
                    if row.get("Outcome") == "WIN":
                        wins += 1

        if total == 0:
            return 100.0, 0
        return (wins / total) * 100.0, total

    def log_trade(self, entry_time: str, exit_time: str, symbol: str, shares: int,
                  entry_price: float, exit_price: float, stop_loss: float,
                  take_profit: float, reason: str):
        pnl = (exit_price - entry_price) * shares
        return_pct = ((exit_price - entry_price) / entry_price) * 100
        outcome = "WIN" if pnl > 0 else "LOSS"
        mode = "PAPER" if PAPER_TRADING else "LIVE"

        with open(self.filepath, mode='a', newline='') as file:
            writer = csv.writer(file)
            writer.writerow([
                entry_time, exit_time, symbol, shares,
                f"{entry_price:.2f}", f"{exit_price:.2f}",
                f"{stop_loss:.2f}", f"{take_profit:.2f}",
                f"{pnl:.2f}", f"{return_pct:.2f}%", outcome, reason, mode
            ])

        win_rate, count = self.get_realized_win_rate()
        print(f"\n[TRADE RECORDED] {symbol} {outcome} | PnL: ${pnl:.2f} ({return_pct:.2f}%)")
        print(f"[AUDIT STATUS] Total Trades: {count} | Current Win Rate: {win_rate:.1f}%\n")

# =====================================================================
# 4. MULTI-STOCK EXECUTION CONTROLLER ($200 HARD CAP)
# =====================================================================
class PortfolioTradingAgent:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self.auditor = PerformanceAuditor(LOG_FILE_PATH)
        
        self.invalid_tickers: Set[str] = self.load_blacklist()
        self.active_universe: List[str] = []

        # Multi-Stock Portfolio tracking: {symbol: ActivePosition}
        self.open_positions: Dict[str, ActivePosition] = {}

        self.daily_trades_executed = 0
        self.current_trading_day = datetime.now().strftime("%Y-%m-%d")

    def load_blacklist(self) -> Set[str]:
        # Pre-seed known defunct or reorganized symbols
        defaults = {"GPS", "WBA", "SAVE", "BITF"}
        if BLACKLIST_FILE_PATH.exists():
            try:
                with open(BLACKLIST_FILE_PATH, "r") as f:
                    file_tickers = set(line.strip().upper() for line in f if line.strip())
                    defaults.update(file_tickers)
            except Exception:
                pass
        return defaults

    def save_to_blacklist(self, bad_syms: Set[str]):
        self.invalid_tickers.update(bad_syms)
        try:
            with open(BLACKLIST_FILE_PATH, "a") as f:
                for sym in bad_syms:
                    f.write(f"{sym}\n")
        except Exception:
            pass

    def login(self):
        print(f"[AUTH] Connecting to Robinhood (Live Mode: {not PAPER_TRADING})...")
        try:
            r.login(
                username=self.username,
                password=self.password,
                expiresIn=86400,
                store_session=True
            )
            print("[AUTH] Successfully authenticated.\n")
        except Exception as e:
            print(f"[LOGIN FAILED] {e}")
            sys.exit(1)

    def load_prefiltered_universe(self) -> List[str]:
        """Loads broad US universe, filtering out invalid/delisted tickers."""
        tickers = []
        if EXCEL_WATCHLIST_PATH.exists():
            print(f"[DATA LOADER] Reading {EXCEL_WATCHLIST_PATH.name}...")
            df = pd.read_excel(EXCEL_WATCHLIST_PATH)
            sym_col = next((c for c in df.columns if c.lower() in ['symbol', 'ticker']), None)
            price_col = next((c for c in df.columns if 'close' in c.lower() or 'price' in c.lower()), None)
            if sym_col and price_col:
                filtered = df[df[price_col] <= MAX_PRICE_LIMIT]
                tickers = filtered[sym_col].dropna().astype(str).str.strip().str.upper().tolist()

        elif CSV_WATCHLIST_PATH.exists():
            print(f"[DATA LOADER] Reading {CSV_WATCHLIST_PATH.name}...")
            df = pd.read_csv(CSV_WATCHLIST_PATH)
            sym_col = next((c for c in df.columns if c.lower() in ['symbol', 'ticker']), None)
            price_col = next((c for c in df.columns if 'close' in c.lower() or 'price' in c.lower()), None)
            if sym_col and price_col:
                filtered = df[df[price_col] <= MAX_PRICE_LIMIT]
                tickers = filtered[sym_col].dropna().astype(str).str.strip().str.upper().tolist()

        if not tickers:
            print("[DATA LOADER] Loading curated multi-sector US liquid universe under $50...")
            # Verified list: GPS, WBA, SAVE, BITF are completely removed
            tickers = [
                # FinTech & Financials
                "SOFI", "HOOD", "AFRM", "NU", "BAC", "BBD", "VALE", "OPEN", "CLOV",
                # Tech, AI, Cloud, Media
                "PLTR", "SNAP", "DKNG", "PINS", "PATH", "GRAB", "WBD", "KVUE", "SOUN", "BBAI",
                # Semis, Hardware, Telecom
                "INTC", "CSCO", "KMI", "BMY", "PFE", "NOK", "ERIC", "LUMN",
                # Clean Tech & EV
                "F", "RIVN", "LCID", "NIO", "RUN", "PLUG", "TLRY", "BLNK", "CHPT",
                # Space & Defense Tech
                "ASTS", "RKLB", "JOBY", "ACHR", "AUR", "PLRX", "DNA",
                # Crypto Proxies & High-Beta Momentum
                "MARA", "RIOT", "CLSK", "HUT", "CIFR",
                # Airlines & Travel
                "AAL", "CCL", "NCLH", "JBLU"
            ]

        cleaned = [t for t in tickers if t not in self.invalid_tickers]
        self.active_universe = cleaned
        return cleaned

    def get_current_invested_capital(self) -> float:
        """Calculates total dollars currently allocated across all open positions."""
        return sum(pos.shares * pos.entry_price for pos in self.open_positions.values())

    def reset_daily_counters_if_new_day(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self.current_trading_day:
            self.current_trading_day = today
            self.daily_trades_executed = 0
            print(f"[SYSTEM] Session reset for {today}. Trades today: 0.")

    def fetch_quotes_chunk(self, chunk: List[str]) -> Tuple[List[dict], Set[str]]:
        bad_tickers = set()
        valid_quotes = []
        try:
            # Silence internal print statements from robin_stocks on invalid tickers
            null_fd = os.open(os.devnull, os.O_WRONLY)
            old_stdout = os.dup(1)
            old_stderr = os.dup(2)
            try:
                os.dup2(null_fd, 1)
                os.dup2(null_fd, 2)
                res = r.get_quotes(chunk)
            finally:
                os.dup2(old_stdout, 1)
                os.dup2(old_stderr, 2)
                os.close(old_stdout)
                os.close(old_stderr)
                os.close(null_fd)

            if not res or len(res) == 0:
                return [], set(chunk)

            for i, q in enumerate(res):
                symbol_queried = chunk[i]
                if q is None or not q.get('last_trade_price'):
                    bad_tickers.add(symbol_queried)
                else:
                    valid_quotes.append(q)

            return valid_quotes, bad_tickers
        except Exception:
            return [], set()

    def fetch_universe_quotes_parallel(self) -> Dict[str, MarketData]:
        data_map: Dict[str, MarketData] = {}
        new_invalids: Set[str] = set()
        chunk_size = 50
        chunks = [self.active_universe[i:i + chunk_size] for i in range(0, len(self.active_universe), chunk_size)]

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = executor.map(self.fetch_quotes_chunk, chunks)
            for quote_list, bad_chunk in results:
                new_invalids.update(bad_chunk)
                for q in quote_list:
                    sym = q['symbol']
                    price = float(q['last_trade_price'])
                    prev_close = float(q.get('previous_close', price))
                    
                    data_map[sym] = MarketData(
                        symbol=sym,
                        high=max(price, prev_close),
                        low=min(price, prev_close),
                        close=price,
                        open=prev_close,
                        volume=0
                    )

        if new_invalids:
            self.save_to_blacklist(new_invalids)
            self.active_universe = [s for s in self.active_universe if s not in self.invalid_tickers]
            print(f"\n[BLACKLIST] Filtered newly discovered invalid tickers: {list(new_invalids)}. Universe: {len(self.active_universe)}\n")

        return data_map

    def execute_entry(self, symbol: str, entry_price: float, stop_loss: float, take_profit: float):
        # 1. Do not duplicate active ticker
        if symbol in self.open_positions:
            return

        # 2. Strict Price ceiling
        if entry_price > MAX_PRICE_LIMIT:
            return

        # 3. Daily trade count cap
        if self.daily_trades_executed >= MAX_TRADES_PER_DAY:
            return

        # 4. Strict $200 Total Exposure Check
        current_invested = self.get_current_invested_capital()
        candidate_cost = SHARES_PER_TRADE * entry_price

        if current_invested + candidate_cost > TOTAL_PORTFOLIO_CAP:
            return

        # 5. 75%+ Win Rate circuit breaker
        win_rate, total_trades = self.auditor.get_realized_win_rate()
        if total_trades >= MIN_SAMPLE_SIZE_TRADES and win_rate < MIN_REQUIRED_WIN_RATE:
            print(f"[SAFETY LOCK] Win rate is {win_rate:.1f}% (< {MIN_REQUIRED_WIN_RATE}%). Execution locked.")
            return

        # Live Order Dispatch
        if not PAPER_TRADING:
            print(f"\n[LIVE ORDER] Routing BUY order for {SHARES_PER_TRADE} share of {symbol} @ ~${entry_price:.2f}...")
            order_res = r.orders.order_buy_market(symbol=symbol, quantity=SHARES_PER_TRADE)
            if not order_res or "id" not in order_res:
                print(f"[ORDER REJECTED] Broker response: {order_res}")
                return
            print(f"[ORDER ACCEPTED] Broker Order ID: {order_res.get('id')} | State: {order_res.get('state')}")

        # Add to open positions portfolio
        self.open_positions[symbol] = ActivePosition(
            symbol=symbol,
            shares=SHARES_PER_TRADE,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.daily_trades_executed += 1

        new_total_invested = self.get_current_invested_capital()
        print(f"\n{'=' * 65}")
        print(f"[{'PAPER' if PAPER_TRADING else 'LIVE REAL-MONEY'} POSITION OPENED: {symbol}]")
        print(f"Shares:           {SHARES_PER_TRADE} share @ ${entry_price:.2f}")
        print(f"Hard Stop:        ${stop_loss:.2f} | Target: ${take_profit:.2f} ({RISK_REWARD_RATIO}R)")
        print(f"Total Invested:   ${new_total_invested:.2f} / ${TOTAL_PORTFOLIO_CAP:.2f} Cap")
        print(f"Open Positions:   {len(self.open_positions)} active stock(s)")
        print(f"Trades Today:     {self.daily_trades_executed}/{MAX_TRADES_PER_DAY}")
        print(f"{'=' * 65}\n")

    def check_exits(self, market_snapshot: Dict[str, MarketData]):
        """Iterates through open positions to check stops and targets."""
        symbols_to_close = []

        for sym, pos in self.open_positions.items():
            if sym not in market_snapshot:
                continue

            current_price = market_snapshot[sym].close
            exit_reason = None

            if current_price <= pos.stop_loss:
                exit_reason = "STOP_LOSS_HIT"
            elif current_price >= pos.take_profit:
                exit_reason = "TAKE_PROFIT_HIT"

            if exit_reason:
                symbols_to_close.append((sym, current_price, exit_reason))

        for sym, exit_price, reason in symbols_to_close:
            pos = self.open_positions[sym]
            exit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            print(f"\n[EXIT TRIGGERED] {sym} hit {reason} at ${exit_price:.2f}!")

            if not PAPER_TRADING:
                print(f"[LIVE ORDER] Submitting SELL order for {pos.shares} share of {sym}...")
                sell_res = r.orders.order_sell_market(symbol=sym, quantity=pos.shares)
                print(f"[SELL RESPONSE] State: {sell_res.get('state')} | Order ID: {sell_res.get('id')}")

            self.auditor.log_trade(
                entry_time=pos.entry_timestamp,
                exit_time=exit_time,
                symbol=sym,
                shares=pos.shares,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                reason=reason
            )

            del self.open_positions[sym]
            print(f"[CAPITAL FREED] Open commitment now: ${self.get_current_invested_capital():.2f} / ${TOTAL_PORTFOLIO_CAP:.2f}\n")

# =====================================================================
# 5. RUNTIME LOOP
# =====================================================================
if __name__ == "__main__":
    agent = PortfolioTradingAgent(
        username=ROBINHOOD_USERNAME,
        password=ROBINHOOD_PASSWORD
    )
    agent.login()

    agent.load_prefiltered_universe()
    print(f"[ACTIVE UNIVERSE] Tracking {len(agent.active_universe)} clean candidate tickers (<= ${MAX_PRICE_LIMIT:.2f}).")
    print(f"[PORTFOLIO CONSTRAINTS] Max Total Exposure: ${TOTAL_PORTFOLIO_CAP:.2f} | Size: {SHARES_PER_TRADE} share per stock.")

    engine = HighSpeedMomentumEngine(atr_periods=14, rsi_periods=14)

    try:
        while True:
            t0 = time.time()
            agent.reset_daily_counters_if_new_day()
            
            # 1. Parallel bulk quotes
            market_snapshot = agent.fetch_universe_quotes_parallel()

            # 2. Update technical indicators
            for sym, bar in market_snapshot.items():
                engine.update_ticker(bar)

            # 3. Manage Open Positions
            if agent.open_positions:
                agent.check_exits(market_snapshot)

            # 4. Print Status of Active Positions
            invested_now = agent.get_current_invested_capital()
            if agent.open_positions:
                pos_summary = ", ".join([f"{s} (${agent.open_positions[s].entry_price:.2f})" for s in agent.open_positions])
                print(f"[ACTIVE PORTFOLIO] ${invested_now:.2f}/${TOTAL_PORTFOLIO_CAP:.2f} invested | Positions: [{pos_summary}]")

            # 5. Scan Universe for New Breakouts (if room remains under the $200 cap)
            if agent.daily_trades_executed < MAX_TRADES_PER_DAY and invested_now < TOTAL_PORTFOLIO_CAP:
                for sym, bar in market_snapshot.items():
                    if sym in agent.open_positions:
                        continue

                    # Check if purchasing 1 share stays within the $200 total cap
                    if (invested_now + bar.close) <= TOTAL_PORTFOLIO_CAP and bar.close <= MAX_PRICE_LIMIT:
                        setup, entry, stop, tp = engine.evaluate_breakout(sym)
                        if setup:
                            agent.execute_entry(sym, entry, stop, tp)
                            invested_now = agent.get_current_invested_capital()
                            if invested_now >= TOTAL_PORTFOLIO_CAP:
                                break

                elapsed = time.time() - t0
                win_pct, trades = agent.auditor.get_realized_win_rate()
                print(f"[FAST SCAN] Scanned {len(market_snapshot)} stocks in {elapsed:.2f}s | Invested: ${invested_now:.2f}/${TOTAL_PORTFOLIO_CAP:.2f} | Today: {agent.daily_trades_executed}/{MAX_TRADES_PER_DAY} | Win Rate: {win_pct:.1f}%")

            elif invested_now >= TOTAL_PORTFOLIO_CAP:
                print(f"[MAX ALLOCATION REACHED] Full ${TOTAL_PORTFOLIO_CAP:.2f} deployed across {len(agent.open_positions)} stocks. Monitoring exits.")
            else:
                print(f"[IDLE] Daily trade cap reached ({MAX_TRADES_PER_DAY}/{MAX_TRADES_PER_DAY}). Standing down.")

            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nStopping algorithm. Logging out...")
        r.logout()