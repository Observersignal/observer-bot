"""
risk.py — pre-trade risk checks. Pure functions, no side effects.

These gate whether the bot is allowed to OPEN a new position. Closes and
flips-to-close are never blocked here (you should always be able to reduce
risk). The checks are intentionally simple and local to the client.
"""

from __future__ import annotations

import os

from config import Config
from state import State

# Kill-switch file. Create it (e.g. `touch STOP`) to stop opening new trades
# immediately, without killing the process.
STOP_FILE = "STOP"


def can_open(state: State, cfg: Config, equity: "float | None" = None) -> "tuple[bool, str]":
    """
    Return (allowed, reason).

    Blocks a new open when:
      - the STOP kill-switch file exists,
      - a recent close's realized PnL is undetermined (daily stop unreliable),
      - the number of open positions is already at MAX_OPEN,
      - the account's live equity is below EQUITY_FLOOR_USD (dynamic sizing only), or
      - today's realized loss has reached the daily loss limit.

    `equity` is the live account value; only consulted with DYNAMIC_SIZING + a floor set.
    """
    if os.path.exists(STOP_FILE):
        return False, f"kill-switch active ({STOP_FILE} file present)"

    # If a live close couldn't determine its realized PnL, the daily-loss stop
    # may be under-counted. Pause new opens until a reconcile confirms the day's
    # realized PnL from the exchange.
    if getattr(state, "pnl_undetermined", False):
        return (
            False,
            "realized PnL of a recent close is undetermined — pausing new opens "
            "until reconciled with the exchange",
        )

    open_count = state.count_open()
    if open_count >= cfg.max_open:
        return False, f"max open positions reached ({open_count}/{cfg.max_open})"

    # Equity floor: below it, stop opening and only close. Dynamic sizing already shrinks
    # every trade with the account, but past a point the positions are dust the exchange
    # rejects anyway, and feeding the last of the account to orders that can't work helps
    # nobody. Only meaningful with dynamic sizing — without it there's no equity to read.
    floor = getattr(cfg, "equity_floor_usd", 0.0) or 0.0
    if getattr(cfg, "dynamic_sizing", False) and floor > 0:
        if equity is None:
            return False, "equity unreadable — pausing new opens (EQUITY_FLOOR_USD is set)"
        if equity < floor:
            return False, f"equity floor reached ({equity:.2f} < {floor:g} USD) — only closing"

    # realized_today_usd is negative when losing. Block once the loss reaches
    # the configured limit.
    if state.realized_today_usd <= -abs(cfg.daily_loss_limit_usd):
        return (
            False,
            f"daily loss limit hit "
            f"({state.realized_today_usd:.2f} <= -{cfg.daily_loss_limit_usd:g} USD)",
        )

    return True, "ok"
