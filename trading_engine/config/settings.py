from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings. Live orders require an explicit opt-in."""

    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "production"] = "development"
    broker: Literal["paper", "alpaca", "zerodha", "robinhood"] = "robinhood"
    live_trading_enabled: bool = False
    database_path: Path = Path("./data/trading_engine.sqlite3")
    universe_path: Path = Path(__file__).with_name("universe.json")
    capital_limit_usd: float = Field(default=2000.0, gt=0, le=2000.0)
    per_trade_risk_fraction: float = Field(default=0.005, gt=0, le=0.02)
    max_open_positions: int = Field(default=5, ge=1, le=20)
    minimum_backtest_trades: int = Field(default=30, ge=1)
    minimum_backtest_win_rate: float = Field(default=0.75, ge=0, le=1)
    history_ttl_seconds: int = Field(default=86400, ge=60)
    request_rate_per_second: float = Field(default=2.0, gt=0, le=20)
    request_burst: int = Field(default=4, ge=1, le=100)
    poll_interval_seconds: int = Field(default=60, ge=5)
    scan_interval_seconds: int = Field(default=900, ge=60, le=3600)
    max_daily_entries: int = Field(default=3, ge=1, le=20)
    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_paper: bool = True
    kite_api_key: SecretStr | None = None
    kite_access_token: SecretStr | None = None
    kite_instrument_map: Path = Path(__file__).with_name("kite_instruments.json")
    kite_capital_limit_inr: float = Field(default=180000.0, gt=0, le=200000.0)
    robinhood_username: SecretStr | None = None
    robinhood_password: SecretStr | None = None
    robinhood_mfa_code: SecretStr | None = None
    robinhood_totp_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_authorized_user_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_telegram_config(self):
        if bool(self.telegram_bot_token) != bool(self.telegram_authorized_user_id):
            raise ValueError("Configure both TELEGRAM_BOT_TOKEN and TELEGRAM_AUTHORIZED_USER_ID, or neither")
        return self

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
