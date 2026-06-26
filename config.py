"""
config.py — load and validate client configuration from the environment.

All configuration lives in the client's own .env file (copied from
.env.example). Nothing is hardcoded; no secrets ever live in this repo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env from the current working directory (the client's machine).
load_dotenv()


def _get_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Config:
    # Signal feed
    api_url: str
    feed_token: str

    # Client's Hyperliquid account
    account_address: str
    api_secret: str

    # Sizing & leverage
    size_usd: float
    leverage: float

    # Risk limits
    max_open: int
    daily_loss_limit_usd: float

    # Safety / behaviour
    dry_run: bool
    poll_seconds: int
    allow_flip: bool
    allow_increase: bool

    @property
    def mock_mode(self) -> bool:
        """No feed token -> run the local DEV mock sequence instead of HTTP."""
        return not self.feed_token

    def configured_live(self) -> bool:
        """
        True only if the bot is allowed to place REAL orders:
        a feed token, an account address and an API secret are all present
        AND dry-run is explicitly disabled.
        """
        return bool(
            self.feed_token
            and self.account_address
            and self.api_secret
            and not self.dry_run
        )

    def summary(self) -> str:
        """Human-readable config dump WITHOUT any secrets."""
        feed = "live feed" if self.feed_token else "DEV MOCK (no token)"
        addr = self.account_address or "(not set)"
        if len(addr) > 12:
            addr = addr[:6] + "..." + addr[-4:]
        secret = "set" if self.api_secret else "(not set)"
        return (
            f"  feed source      : {feed}\n"
            f"  api url          : {self.api_url}\n"
            f"  account address  : {addr}\n"
            f"  api secret       : {secret}\n"
            f"  size per trade   : {self.size_usd:g} USD margin "
            f"@ {self.leverage:g}x leverage\n"
            f"  max open         : {self.max_open}\n"
            f"  daily loss limit : {self.daily_loss_limit_usd:g} USD\n"
            f"  poll interval    : {self.poll_seconds}s\n"
            f"  allow flip       : {self.allow_flip}\n"
            f"  allow increase   : {self.allow_increase}\n"
            f"  dry run          : {self.dry_run}"
        )


def load_config() -> Config:
    return Config(
        api_url=_get_str("OBSERVER_API_URL", "https://www.theobserversignalbot.com"),
        feed_token=_get_str("OBSERVER_FEED_TOKEN"),
        account_address=_get_str("HL_ACCOUNT_ADDRESS"),
        api_secret=_get_str("HL_API_SECRET"),
        size_usd=_get_float("SIZE_USD", 100.0),
        leverage=_get_float("LEVERAGE", 10.0),
        max_open=_get_int("MAX_OPEN", 10),
        daily_loss_limit_usd=_get_float("DAILY_LOSS_LIMIT_USD", 300.0),
        dry_run=_get_bool("DRY_RUN", True),
        poll_seconds=_get_int("POLL_SECONDS", 4),
        allow_flip=_get_bool("ALLOW_FLIP", True),
        allow_increase=_get_bool("ALLOW_INCREASE", False),
    )


# Module-level singleton so other modules can `from config import CFG`.
CFG: Config = load_config()
