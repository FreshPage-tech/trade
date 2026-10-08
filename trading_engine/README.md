# Multi broker quantitative trading engine

The package keeps the existing chart pattern detection, EMA/RSI/ATR confluence, inventory skew, rolling backtest, risk sizing, SQLite bar cache, audit log, and managed-position lifecycle behind one broker interface. Choose a broker at the command line; credentials stay in `trading_engine/.env`.

## Commands

Run these from the directory containing `trading_engine/`:

```bash
python run.py robinhood
python run.py alpaca
python run.py kite

python status.py robinhood
python status.py alpaca
python status.py kite
```

Aliases `rh` and `zerodha` are accepted. Equivalent package commands are `python -m trading_engine run robinhood` and `python -m trading_engine status robinhood`. Run commands start the continuous daemon; status commands authenticate and read equity, positions, open buy commitment, and remaining engine allocation, then exit.

**Live mode:** `LIVE_TRADING_ENABLED=true` allows the selected live adapter to submit real orders. The current private `.env` is configured for Robinhood live mode as requested. Therefore `python run.py robinhood` can place real orders. The status command does not submit orders. Do not start a run command until you are ready for that broker to trade. `LIVE_TRADING_ENABLED=false` disables entries for every broker.

Each selected broker has a separate SQLite state file (Robinhood retains the configured `DATABASE_PATH`; other brokers add `_alpaca` or `_zerodha`). This avoids sharing managed order state across accounts. Run only one daemon per broker account at a time.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r trading_engine/requirements.txt
python run.py robinhood
```

Use the same activated `.venv` to run the commands. Installing with a different `pip`/Python is a common cause of `ModuleNotFoundError: No module named 'pydantic'`.

Copy `.env.example` to `trading_engine/.env` only if you need a template; retain existing credentials without copying them into source or chat. On Linux, restrict the file with `chmod 600 trading_engine/.env`.

### Broker configuration

- **Robinhood:** set `ROBINHOOD_USERNAME` and `ROBINHOOD_PASSWORD`. For unattended MFA renewal, set `ROBINHOOD_TOTP_SECRET`; a one-time `ROBINHOOD_MFA_CODE` expires. Uses the unofficial `robin_stocks` client and supports the existing US stock/ETF strategy path.
- **Alpaca:** set `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. `ALPACA_PAPER=true` routes to Alpaca paper account; set it to `false` for the live account. `LIVE_TRADING_ENABLED` must also be true before the engine submits entries.
- **Kite / Zerodha:** set `KITE_API_KEY` and the currently valid `KITE_ACCESS_TOKEN` (Kite tokens need renewal through the broker flow). Fill `config/kite_instruments.json` with tradable symbols and their exchange/instrument token. `KITE_CAPITAL_LIMIT_INR` is independently capped at ₹200,000 (default ₹180,000); the $2,000 USD setting does not apply to Kite.

The command-line broker selection overrides `BROKER` from `.env`; all credentials and safety/risk settings still come from that file. Alpaca is selected with `alpaca`; Kite is stored internally as `zerodha`.

## Telegram remote control

Set both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_AUTHORIZED_USER_ID` in `trading_engine/.env`. The authorized ID is the numeric Telegram user ID, not a username. Send `/start` to the bot once so it can send private messages. The daemon starts Telegram polling automatically when both settings are configured. Only that user in a private chat can use the bot. Available commands are `/status`, `/positions`, `/review`, `/halt`, `/resume`, and `/help`. `/halt` stops new strategy entries while broker positions continue to be managed; the halt state survives process restarts. There are no Telegram buy, sell, panic-liquidation, or live-mode-toggle commands.

The bot uses Telegram long polling, which makes outbound HTTPS requests from the VM and needs no inbound Telegram webhook port. It sends a reconciliation review at 6:00 PM in the exchange timezone on trading days; `/review` runs one immediately. The review compares broker positions with the managed plan, reports manual/unmanaged positions and quantity changes, and includes history/scan counts. Manual additions are included in capital exposure but are not auto-adopted into the stop/target plan. Manual closes trigger cancellation of their old protective stop. A changed managed quantity halts new entries until reviewed. For round-the-clock access, run the daemon on an always-on OCI VM under systemd; a sleeping Mac cannot provide 24/7 availability. Use `sudo systemctl status trading_bot` and `sudo journalctl -u trading_bot -f` over SSH for host-level checks.

## Strategy and controls

The daemon reconciles positions and monitors protective orders each minute. It runs a scan immediately when started during an open session, then repeats the quant/pattern scan every `SCAN_INTERVAL_SECONDS` (default 900 seconds). If it missed a session, it can run one after-hours watch-only scan. For each eligible symbol it checks SQLite first, fetches up to five years of daily bars on a cache miss, logs bar counts/cache hits, runs the rolling backtest, then applies chart-pattern and indicator filters. Daily bar history is cached for 24 hours by default to reduce broker requests; current quotes are refreshed independently for the intraday trigger check. The default qualification requires 30 backtest trades and a 75% historical win rate. Entry sizing enforces the selected account-currency cap, per-trade risk budget, position count, open exposure, and open buy commitment. Candidates target a 1:2 to 1:4 reward/risk bracket. Telegram watch alerts are sent when live entries are disabled, the market is closed, risk checks fail, or entries are halted.

The status report's allocation is an engine guard, not a broker sub-account. Existing valued positions count against it. Robinhood option positions and open option/crypto buy orders are reported as risk warnings; allocation is shown as unknown and new strategy entries are blocked while the engine cannot value that exposure. The strategy does not adopt pre-existing positions into its stop/target lifecycle. Stop and market orders can execute at prices different from modeled levels; backtests do not guarantee future results.

Kite stop orders in this adapter use a regular DAY stop-market order, so they can expire at the end of the session. Robinhood and Alpaca stop orders use GTC. Check broker-side protection and reconcile positions after restarts; do not rely on daemon monitoring alone for a durable target exit.

Robinhood access uses a community-maintained private API client rather than an official broker API. Authentication and endpoints can change. `trading_engine/telegram_trader.py` is now only a compatibility launcher for the same daemon; do not run it at the same time as `run.py`. Do not run `trade1.py`, which has a separate order and state flow.

## OCI systemd

Install the package under `/opt/trading_engine`, create the virtual environment on the OCI VM (do not copy a Mac `.venv` to Linux), install `/opt/trading_engine/requirements.txt` into `/opt/trading_engine/.venv`, and configure `/opt/trading_engine/.env` with permissions `0600` and owner `trader`. The provided `systemd/trading_bot.service` runs the broker selected in that environment file; set `BROKER` and `LIVE_TRADING_ENABLED` deliberately before enabling it. The service uses `/var/lib/trading_engine` for its SQLite state and Robinhood session. Copy the unit to `/etc/systemd/system/trading_bot.service`, then run `sudo systemctl daemon-reload` and `sudo systemctl enable trading_bot`. Start it only after confirming account configuration and live order settings with `sudo systemctl start trading_bot`. Verify with `sudo systemctl status trading_bot` and inspect history/backtest activity using `sudo journalctl -u trading_bot -f`. The unit restarts the service after failures and at VM reboot.

On Ubuntu, after copying the source and `.env` securely to `/opt/trading_engine`:

```bash
sudo apt update
sudo apt install -y python3-venv
sudo useradd --system --home-dir /var/lib/trading_engine --create-home --shell /usr/sbin/nologin trader
sudo chown -R trader:trader /opt/trading_engine /var/lib/trading_engine
sudo -u trader python3 -m venv /opt/trading_engine/.venv
sudo -u trader /opt/trading_engine/.venv/bin/python -m pip install --upgrade pip
sudo -u trader /opt/trading_engine/.venv/bin/python -m pip install -r /opt/trading_engine/requirements.txt
sudo chmod 600 /opt/trading_engine/.env
sudo cp /opt/trading_engine/systemd/trading_bot.service /etc/systemd/system/trading_bot.service
sudo systemctl daemon-reload
sudo systemctl enable trading_bot
```

The setup enables startup at boot but leaves the service stopped. Start it with `sudo systemctl start trading_bot` only after verifying the account settings and whether live order submission is enabled. Check the service log for `daemon started`, `Telegram remote control started`, `Historical cache miss`, `Historical bars fetched`, and `Quant scan complete`. Open the Telegram bot and send `/start`; the daemon will then deliver alerts and the 6 PM review. OCI egress access to Robinhood and Telegram is required; no inbound bot port is used.
