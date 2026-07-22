"""Decrease unit-tracking and reconcile visibility.

Why these exist: on 23-Jul-2026 an account with ALLOW_INCREASE=false showed BTC tracked
as units=3 while holding one unit's worth. The decrease handler synced `units` UP to the
model's copy count on "target >= held", so later decreases trimmed fractions of a position
the bot never scaled up, leaving odd sub-unit sizes. Separately, reconcile silently adopted
two HL positions that predated the current state file (AVAX, BCH); their close signals were
already behind the cursor, so they sat unmanaged — sized under an older config — until the
owner found and closed them by hand.

These pin: (a) `units` tracks what WE opened, never the model's count; (b) trims below our
held count still work; (c) reconcile adoption/drop of untracked positions is loud.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from config import Config
from state import State


def cfg(**over):
    """A Config with the fields these tests care about; the rest are inert defaults."""
    base = dict(
        api_url="", feed_token="", account_address="0xabc", api_secret="",
        sizing_mode="model",
        base_capital=1500.0, risk_per_trade_pct=1.0,
        size_usd=150.0, base_margin_usd=150.0,
        dynamic_sizing=False, equity_floor_usd=0.0,
        leverage=10.0, isolated=True,
        max_open=25, daily_loss_limit_usd=300.0,
        dry_run=True, poll_seconds=4, allow_flip=True, allow_increase=False,
        max_signal_age_min=15.0, max_slippage_pct=0.01, reconcile_every=20,
    )
    base.update(over)
    return Config(**base)


class FakeExecutor:
    """Records reduce/close calls; returns ok so state updates run."""

    def __init__(self):
        self.reduced = []   # (coin, fraction)
        self.closed = []    # coin

    def reduce(self, coin, fraction):
        self.reduced.append((coin, fraction))
        return {"ok": True, "coin": coin}

    def close(self, coin):
        self.closed.append(coin)
        return {"ok": True, "coin": coin}

    def account_value(self):
        return 1000.0


def ev_decrease(coin="BTC", side="long", units=1):
    return {"id": "1", "event": "decrease", "coin": coin, "side": side, "units": units}


# --- decrease must not inflate our unit count --------------------------------------

def test_decrease_above_held_does_not_sync_units_up():
    """Model 3 -> 2 copies while we hold 1 unit (increases skipped): nothing to trim,
    and our count stays 1 — inflating it made later decreases trim a position we
    never scaled up."""
    st = State({"open_positions": {"BTC": {"side": "long", "units": 1}}})
    ex = FakeExecutor()
    bot._handle_event(ev_decrease(units=2), st, ex, cfg())
    assert ex.reduced == []
    assert st.units_of("BTC") == 1


def test_decrease_below_held_still_trims_and_syncs_down():
    """We really hold 3 units (increases were on): 3 -> 1 trims 2/3 and tracks 1."""
    st = State({"open_positions": {"BTC": {"side": "long", "units": 3}}})
    ex = FakeExecutor()
    bot._handle_event(ev_decrease(units=1), st, ex, cfg(allow_increase=True))
    assert len(ex.reduced) == 1
    coin, fraction = ex.reduced[0]
    assert coin == "BTC"
    assert abs(fraction - 2.0 / 3.0) < 1e-9
    assert st.units_of("BTC") == 1


def test_decrease_to_zero_closes_fully():
    st = State({"open_positions": {"BTC": {"side": "long", "units": 1}}})
    ex = FakeExecutor()
    bot._handle_event(ev_decrease(units=0), st, ex, cfg())
    assert ex.closed == ["BTC"]
    assert st.units_of("BTC") == 0


# --- reconcile adoption/drop must be loud ------------------------------------------

def test_reconcile_adopting_untracked_position_warns(caplog):
    """An HL position we never tracked is adopted as 1 unit AND warned about: its
    close signal may already be behind the cursor and never arrive."""
    st = State({"open_positions": {"BTC": {"side": "long", "units": 2}}})
    with caplog.at_level(logging.WARNING, logger="observer.state"):
        st.apply_reconciliation({"BTC": "long", "AVAX": "short"}, 0.0)
    assert st.units_of("AVAX") == 1
    assert st.units_of("BTC") == 2   # same-side tracked position keeps its units
    warned = [r for r in caplog.records if "AVAX" in r.getMessage()]
    assert warned and warned[0].levelno == logging.WARNING


def test_reconcile_dropping_flat_position_logs(caplog):
    """A tracked position HL shows flat (manual close/liquidation) is dropped with a log."""
    st = State({"open_positions": {"BCH": {"side": "long", "units": 1}}})
    with caplog.at_level(logging.INFO, logger="observer.state"):
        st.apply_reconciliation({}, 0.0)
    assert st.units_of("BCH") == 0
    assert any("BCH" in r.getMessage() for r in caplog.records)
