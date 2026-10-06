import os
import sys
from pathlib import Path
import robin_stocks.robinhood as r
from dotenv import load_dotenv

# Load credentials from .env
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME")
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD")

if not all([ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD]):
    print("\n[ERROR] Missing credentials in .env!\n")
    sys.exit(1)

def display_status():
    print("\n" + "=" * 65)
    print("        ROBINHOOD REAL-TIME PORTFOLIO & POSITION AUDIT        ")
    print("=" * 65)

    try:
        r.login(ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, expiresIn=86400, store_session=True)
    except Exception as e:
        print(f"[AUTH FAILED] {e}")
        return

    # 1. Fetch Account Summary / Buying Power
    profile = r.profiles.load_account_profile()
    buying_power = float(profile.get("buying_power", 0.0) or profile.get("cash", 0.0))
    portfolio = r.profiles.load_portfolio_profile()
    total_equity = float(portfolio.get("equity", 0.0))

    print(f"Total Account Equity: ${total_equity:,.2f}")
    print(f"Available Cash/BP:   ${buying_power:,.2f}")
    print("-" * 65)

    # 2. Check Open Stock / ETF Positions
    stock_positions = r.account.get_open_stock_positions()
    total_stock_value = 0.0
    active_stock_count = 0

    print(f"\n[EQUITY & ETF POSITIONS]")
    if stock_positions:
        for pos in stock_positions:
            qty = float(pos.get("quantity", 0.0))
            if qty <= 0:
                continue

            active_stock_count += 1
            instrument_url = pos.get("instrument")
            instrument_data = r.helper.request_get(instrument_url)
            symbol = instrument_data.get("symbol")

            avg_buy_price = float(pos.get("average_buy_price", 0.0))
            quote = r.stocks.get_latest_price(symbol)[0]
            current_price = float(quote) if quote else avg_buy_price

            market_val = qty * current_price
            cost_basis = qty * avg_buy_price
            pnl_dollars = market_val - cost_basis
            pnl_pct = (pnl_dollars / cost_basis * 100) if cost_basis > 0 else 0.0

            total_stock_value += market_val
            pnl_sign = "+" if pnl_dollars >= 0 else ""

            print(f" • {symbol:<6} | Qty: {qty:>6.2f} | Entry: ${avg_buy_price:>7.2f} | Current: ${current_price:>7.2f} | Value: ${market_val:>8.2f} | PnL: {pnl_sign}${pnl_dollars:.2f} ({pnl_sign}{pnl_pct:.2f}%)")
    
    if active_stock_count == 0:
        print("  (No open stock/ETF positions found)")

    # 3. Check Open Crypto Positions
    total_crypto_value = 0.0
    active_crypto_count = 0
    try:
        crypto_positions = r.crypto.get_crypto_positions()
        print(f"\n[CRYPTO POSITIONS]")
        if crypto_positions:
            for c_pos in crypto_positions:
                qty = float(c_pos.get("quantity_available", 0.0))
                if qty <= 0:
                    continue

                active_crypto_count += 1
                curr_info = c_pos.get("currency", {})
                code = curr_info.get("code")
                quote = r.crypto.get_crypto_quote(code)
                mark_price = float(quote.get("mark_price", 0.0)) if quote else 0.0
                market_val = qty * mark_price
                total_crypto_value += market_val

                print(f" • {code:<6} | Qty: {qty:>10.4f} | Mark: ${mark_price:>9.2f} | Value: ${market_val:>8.2f}")
    except Exception:
        pass

    if active_crypto_count == 0:
        print("  (No open crypto positions found)")

    total_deployed = total_stock_value + total_crypto_value
    print("\n" + "-" * 65)
    print(f"TOTAL CAPITAL CURRENTLY DEPLOYED : ${total_deployed:,.2f} / $2,000.00 Limit")
    remaining_room = max(0.0, 2000.00 - total_deployed)
    print(f"REMAINING ALLOCATION AVAILABLE   : ${remaining_room:,.2f}")
    print("=" * 65 + "\n")

if __name__ == "__main__":
    display_status()