"""Dynamic sizing: the ×1.0 base is a % of LIVE equity, not of a typed BASE_CAPITAL.

Why these exist: on 17-Jul-2026 an account configured with BASE_CAPITAL=1500 had drifted
to 783 USD of real equity. The bot kept sizing every open for 1500 (large = 225 USD margin
@10x = 2250 notional — 287% of the actual account), reached 100% margin used, and started
getting "Insufficient margin" rejections and 55-61% partial fills. There is no exposure cap in this
bot at all, so nothing stopped it. Nothing was broken; every number was simply measured
against a capital that no longer existed.

These tests pin the fix AND the old behaviour (which stays the default: this bot runs on
clients' machines and their sizing must not change under them).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import risk
import sizing
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


# --- static sizing (the default) must not move ------------------------------------

def test_static_sizing_is_unchanged_by_default():
    """Existing clients keep their exact behaviour: equity is ignored when the flag is off."""
    margin, lev, _ = sizing.plan_open("BTC", cfg(), {"tier": "large"}, equity=783.61)
    assert margin == 225.0          # 150 base x1.5 large
    assert lev == 10.0
    margin, lev, _ = sizing.plan_open("OP", cfg(), {"tier": "small"}, equity=783.61)
    assert margin == 75.0           # 150 base x0.5 small
    assert lev == 5.0


# --- dynamic sizing ---------------------------------------------------------------

def test_dynamic_sizes_off_live_equity_with_tiers():
    """1% of 783.61 = 7.8361 base; large x1.5 @10x, small x0.5 @5x."""
    c = cfg(dynamic_sizing=True, risk_per_trade_pct=1.0)
    margin, lev, why = sizing.plan_open("BTC", c, {"tier": "large"}, equity=783.61)
    assert margin == pytest.approx(11.754, rel=1e-3)
    assert lev == 10.0
    assert "783.61" in why          # the reason must show what it sized off

    margin, lev, _ = sizing.plan_open("OP", c, {"tier": "small"}, equity=783.61)
    assert margin == pytest.approx(3.918, rel=1e-3)
    assert lev == 5.0               # small-caps stay at 5x — the tier ceiling still rules


def test_dynamic_breathes_with_the_account():
    """The whole point: bigger when winning, smaller when losing. Same config, same signal."""
    c = cfg(dynamic_sizing=True, risk_per_trade_pct=1.0)
    good, _, _ = sizing.plan_open("BTC", c, {"tier": "large"}, equity=5000.0)
    bad, _, _ = sizing.plan_open("BTC", c, {"tier": "large"}, equity=500.0)
    assert good == pytest.approx(75.0)
    assert bad == pytest.approx(7.5)
    assert good == 10 * bad


def test_dynamic_refuses_when_equity_unreadable():
    """Fail CLOSED. Falling back to the static base is exactly the bug being fixed."""
    c = cfg(dynamic_sizing=True)
    margin, lev, why = sizing.plan_open("BTC", c, {"tier": "large"}, equity=None)
    assert margin is None and lev is None
    assert "unreadable" in why


def test_dynamic_refuses_below_equity_floor():
    c = cfg(dynamic_sizing=True, equity_floor_usd=300.0)
    margin, _, why = sizing.plan_open("BTC", c, {"tier": "large"}, equity=299.0)
    assert margin is None
    assert "floor" in why
    margin, _, _ = sizing.plan_open("BTC", c, {"tier": "large"}, equity=301.0)
    assert margin is not None


def test_micro_caps_still_never_traded():
    c = cfg(dynamic_sizing=True)
    margin, _, why = sizing.plan_open("PEPE", c, {"tier": "micro"}, equity=10_000.0)
    assert margin is None
    assert "micro" in why


# --- the risk gate -------------------------------------------------

def _state():
    """A clean state: nothing open, no realized loss, so only the gate under test fires."""
    return State({})


def test_equity_floor_blocks_opens_in_risk_gate():
    c = cfg(dynamic_sizing=True, equity_floor_usd=300.0)
    allowed, why = risk.can_open(_state(), c, equity=250.0)
    assert allowed is False
    assert "floor" in why


def test_equity_floor_pauses_when_equity_unreadable():
    """No reading + a floor set: pause rather than guess."""
    c = cfg(dynamic_sizing=True, equity_floor_usd=300.0)
    allowed, why = risk.can_open(_state(), c, equity=None)
    assert allowed is False
    assert "unreadable" in why


def test_static_client_unaffected_by_missing_equity():
    """A client without dynamic sizing never reads equity; None must not block them."""
    c = cfg(dynamic_sizing=False, equity_floor_usd=0.0)
    allowed, _ = risk.can_open(_state(), c, equity=None)
    assert allowed is True
