"""Unified-account equity: the perps margin summary is NOT the account.

Why these exist: on 22-Jul-2026 an account in Hyperliquid's "unifiedAccount" mode
held ~905 USD of USDC, but only the ~199 USD backing its open positions showed up
in marginSummary.accountValue — the free 706 USD lives in the SPOT clearinghouse
under unified accounting. The bot read the perps number alone, decided
199 < EQUITY_FLOOR_USD=300, and silently skipped every OPEN for a day and a half
while closes kept working. Nothing was down; the equity was simply being read
from the wrong pocket.

These tests pin the fix: unified equity = perps accountValue + free spot USDC,
classic accounts keep the old reading, and a unified account whose spot side
can't be read fails CLOSED (None) instead of reporting a lowball.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import executor
from config import Config


def cfg(**over):
    """A Config with the fields these tests care about; the rest are inert defaults."""
    base = dict(
        api_url="", feed_token="", account_address="0xabc", api_secret="",
        sizing_mode="model",
        base_capital=1500.0, risk_per_trade_pct=1.0,
        size_usd=150.0, base_margin_usd=150.0,
        dynamic_sizing=True, equity_floor_usd=300.0,
        leverage=10.0, isolated=True,
        max_open=25, daily_loss_limit_usd=300.0,
        dry_run=True, poll_seconds=4, allow_flip=True, allow_increase=False,
        max_signal_age_min=15.0, max_slippage_pct=0.01, reconcile_every=20,
    )
    base.update(over)
    return Config(**base)


class FakeInfo:
    """Answers exactly the three reads account_value() makes, in API shapes."""

    def __init__(self, mode="unifiedAccount", perps="199.15",
                 spot_total="905.46", spot_hold="198.81",
                 mode_raises=False, spot_raises=False):
        self.mode = mode
        self.perps = perps
        self.spot_total = spot_total
        self.spot_hold = spot_hold
        self.mode_raises = mode_raises
        self.spot_raises = spot_raises
        self.mode_calls = 0

    def user_state(self, addr):
        return {"marginSummary": {"accountValue": self.perps}}

    def post(self, path, payload):
        kind = payload.get("type")
        if kind == "userAbstraction":
            self.mode_calls += 1
            if self.mode_raises:
                raise RuntimeError("network down")
            return self.mode
        if kind == "spotClearinghouseState":
            if self.spot_raises:
                raise RuntimeError("network down")
            return {"balances": [
                {"coin": "USDE", "total": "0.0", "hold": "0.0"},
                {"coin": "USDC", "total": self.spot_total, "hold": self.spot_hold},
            ]}
        raise AssertionError(f"unexpected info request: {payload!r}")


def make_executor(monkeypatch, fake):
    # Keep __init__ off the network regardless of whether the SDK is installed.
    monkeypatch.setattr(executor, "_SDK_OK", False, raising=False)
    monkeypatch.setattr(executor, "Info", None, raising=False)
    ex = executor.Executor(cfg(), base_url="http://unit-test.invalid")
    ex.info = fake
    return ex


def test_unified_account_counts_free_spot_usdc(monkeypatch):
    """The 22-Jul bug: 199 of perps margin + 706 free in spot IS a 905 account."""
    ex = make_executor(monkeypatch, FakeInfo())
    assert ex.account_value() == pytest.approx(199.15 + (905.46 - 198.81))


def test_classic_account_keeps_perps_reading(monkeypatch):
    """Accounts not in unified/portfolio mode must not change under anyone."""
    ex = make_executor(monkeypatch, FakeInfo(mode="standard"))
    assert ex.account_value() == pytest.approx(199.15)


def test_mode_unreadable_falls_back_to_perps_and_retries(monkeypatch):
    """A failed mode query behaves classic (pre-fix) and is retried next read."""
    fake = FakeInfo(mode_raises=True)
    ex = make_executor(monkeypatch, fake)
    assert ex.account_value() == pytest.approx(199.15)
    fake.mode_raises = False
    assert ex.account_value() == pytest.approx(199.15 + (905.46 - 198.81))


def test_unified_with_spot_unreadable_fails_closed(monkeypatch):
    """Half a number is not an honest equity: report None, never the lowball."""
    ex = make_executor(monkeypatch, FakeInfo(spot_raises=True))
    assert ex.account_value() is None


def test_mode_is_cached_after_first_success(monkeypatch):
    fake = FakeInfo()
    ex = make_executor(monkeypatch, fake)
    ex.account_value()
    ex.account_value()
    assert fake.mode_calls == 1


def test_unified_with_no_usdc_row_adds_nothing(monkeypatch):
    """No USDC balance row means 0 free spot, not an error."""
    fake = FakeInfo()
    fake.post_orig = fake.post

    def post(path, payload):
        if payload.get("type") == "spotClearinghouseState":
            return {"balances": [{"coin": "USDE", "total": "5.0", "hold": "0.0"}]}
        return fake.post_orig(path, payload)

    fake.post = post
    ex = make_executor(monkeypatch, fake)
    assert ex.account_value() == pytest.approx(199.15)
