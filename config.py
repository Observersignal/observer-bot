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


# Keys this app knows about — used by the UI to write .env safely (it only
# touches these, preserving any other lines the user may have added).
KNOWN_ENV_KEYS = (
    "OBSERVER_API_URL",
    "OBSERVER_FEED_TOKEN",
    "HL_ACCOUNT_ADDRESS",
    "HL_API_SECRET",
    "BASE_CAPITAL",
    "RISK_PER_TRADE_PCT",
    "LEVERAGE",
    "ISOLATED",
    "SIZE_USD",
    "MAX_OPEN",
    "DAILY_LOSS_LIMIT_USD",
    "DRY_RUN",
    "POLL_SECONDS",
    "ALLOW_FLIP",
    "ALLOW_INCREASE",
    "MAX_SLIPPAGE_PCT",
    "RECONCILE_EVERY",
)

# The secret keys that must never be echoed back to the UI in clear text.
SECRET_ENV_KEYS = ("HL_API_SECRET", "OBSERVER_FEED_TOKEN")


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
    base_capital: float        # capital base total del cliente (USD)
    risk_per_trade_pct: float  # % del capital base por operación (1.0 = misma proporción que el modelo)
    size_usd: float            # margen por op derivado = base_capital * pct/100 (o SIZE_USD si se fuerza)
    leverage: float
    isolated: bool             # margen isolated (True) vs cross (False)

    # Risk limits
    max_open: int
    daily_loss_limit_usd: float

    # Safety / behaviour
    dry_run: bool
    poll_seconds: int
    allow_flip: bool
    allow_increase: bool
    max_signal_age_min: float  # on reconnect, OPEN signals older than this are skipped
    max_slippage_pct: float    # market-order slippage cap (fraction: 0.01 = 1%)
    reconcile_every: int       # reconcile local state vs HL every N cycles (0 = off)

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
            f"  base capital     : {self.base_capital:g} USD\n"
            f"  size per trade   : {self.size_usd:g} USD margin "
            f"({self.risk_per_trade_pct:g}% of base) @ {self.leverage:g}x "
            f"{'isolated' if self.isolated else 'cross'}\n"
            f"  max open         : {self.max_open}\n"
            f"  daily loss limit : {self.daily_loss_limit_usd:g} USD\n"
            f"  poll interval    : {self.poll_seconds}s\n"
            f"  allow flip       : {self.allow_flip}\n"
            f"  allow increase   : {self.allow_increase}\n"
            f"  max signal age   : {self.max_signal_age_min:g} min (stale OPENs skipped on reconnect)\n"
            f"  max slippage     : {self.max_slippage_pct * 100:g}% (market orders rejected beyond this)\n"
            f"  reconcile every  : {self.reconcile_every} cycles (0 = off)\n"
            f"  dry run          : {self.dry_run}"
        )


def load_config() -> Config:
    # Sizing PROPORCIONAL: el cliente solo mete su capital base y el bot replica TU proporción
    # ($100 sobre $10.000 = 1% por op). 5.000 -> 50/op; 20.000 -> 200/op. Sin el cut del 50%.
    base_capital = _get_float("BASE_CAPITAL", 10000.0)
    risk_pct = _get_float("RISK_PER_TRADE_PCT", 1.0)
    size_override = _get_float("SIZE_USD", 0.0)  # 0 = derivar del capital base (avanzado: forzar $ fijos)
    size_usd = size_override if size_override > 0 else round(base_capital * risk_pct / 100.0, 2)
    return Config(
        api_url=_get_str("OBSERVER_API_URL", "https://api.theobserversignalbot.com"),
        feed_token=_get_str("OBSERVER_FEED_TOKEN"),
        account_address=_get_str("HL_ACCOUNT_ADDRESS"),
        api_secret=_get_str("HL_API_SECRET"),
        base_capital=base_capital,
        risk_per_trade_pct=risk_pct,
        size_usd=size_usd,
        leverage=_get_float("LEVERAGE", 10.0),
        isolated=_get_bool("ISOLATED", True),
        max_open=_get_int("MAX_OPEN", 10),
        daily_loss_limit_usd=_get_float("DAILY_LOSS_LIMIT_USD", 300.0),
        dry_run=_get_bool("DRY_RUN", True),
        poll_seconds=_get_int("POLL_SECONDS", 4),
        allow_flip=_get_bool("ALLOW_FLIP", True),
        allow_increase=_get_bool("ALLOW_INCREASE", False),
        max_signal_age_min=_get_float("MAX_SIGNAL_AGE_MIN", 15.0),
        max_slippage_pct=_get_float("MAX_SLIPPAGE_PCT", 0.01),
        reconcile_every=_get_int("RECONCILE_EVERY", 20),
    )


# Module-level singleton so other modules can `from config import CFG`.
#
# NOTE: modules that need to pick up live changes after a config save should
# read `config.CFG` at call-time (e.g. `import config; config.CFG.x`) rather
# than binding the name at import-time, because `reload()` rebinds this global.
CFG: Config = load_config()


def reload() -> "Config":
    """
    Re-read the client's .env from disk and rebuild the CFG singleton.

    Called by the UI after a config save so the next Start picks up the new
    values. `load_dotenv(override=True)` re-applies the file over os.environ.
    Returns the freshly-built Config.
    """
    global CFG
    load_dotenv(override=True)
    CFG = load_config()
    return CFG
