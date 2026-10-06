# Trade

Python scripts for Robinhood trading, portfolio status, and Telegram control.

## Setup

1. Create and activate a virtual environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. Create a local `.env` file with the variables required by the script you plan to run:

   ```dotenv
   ROBINHOOD_USERNAME=your_username
   ROBINHOOD_PASSWORD=your_password
   TELEGRAM_BOT_TOKEN=your_bot_token
   TELEGRAM_AUTHORIZED_USER_ID=your_telegram_user_id
   ```

4. Run `python status.py`, `python trade.py`, or `python telegram_trader.py` as needed.

Keep `.env` private. The trading scripts are configured for live trading; review them and understand their behavior before running them with a funded account. `telegram_trader.py` includes a panic command that submits market sell orders for open stock positions.
