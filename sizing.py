"""
sizing.py — decide the margin and leverage for a signal. Pure functions.

Two modes (SIZING_MODE):

  fixed  — every signal uses your configured SIZE_USD margin and LEVERAGE.
           (Micro-caps are still skipped — see below.)

  model  — mirror The Observer model's market-cap-tier policy, scaled to YOUR
           base margin (BASE_MARGIN_USD). The model sizes by tier:
               large (cap ≥ $1B)   → margin ×1.5, 10x
               small ($100M–$1B)   → margin ×0.5, 5x
               micro (< $100M)     → NOT traded
           so with BASE_MARGIN_USD=$100 you get large=$150@10x, small=$50@5x.

Micro-caps (< $100M) are NEVER traded, in either mode: the model refuses them
(worst historical win rate; every past liquidation was below large-cap), and a
client mirroring the model should not silently take that bucket.

Source of the tier decision, in priority order:
  1. the signal itself, if the feed carries `tier`/`size_mult`/`lev` (the model
     is then authoritative and any policy change propagates with no re-download);
  2. otherwise the bundled static table (coin_tiers.py) + the model's default
     multipliers below.
"""

from __future__ import annotations

from typing import Optional

from coin_tiers import TIER_MICRO, TIER_SMALL, tier_of
from config import Config

# The Observer model's per-tier policy (defaults; the feed may override per
# signal). Kept here so a client can read exactly how the mirror sizes.
_TIER_MULT = {"large": 1.5, "small": 0.5, "micro": 0.0}
_TIER_LEV = {"large": 10.0, "small": 5.0}   # micro not traded


def _tier_for(coin: str, signal: dict) -> str:
    """Prefer the feed's tier (authoritative), else the bundled table."""
    t = signal.get("tier")
    if isinstance(t, str) and t:
        return t
    return tier_of(coin, default=TIER_SMALL)


def _mult_for(tier: str, signal: dict) -> float:
    v = signal.get("size_mult")
    if isinstance(v, (int, float)):
        return float(v)
    return _TIER_MULT.get(tier, _TIER_MULT[TIER_SMALL])


def _lev_for(tier: str, signal: dict, fallback: float) -> float:
    v = signal.get("lev")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    return _TIER_LEV.get(tier, fallback)


def _base_margin(cfg: Config, equity: Optional[float]) -> "tuple[Optional[float], str]":
    """
    The ×1.0 reference margin a tier multiplier is applied to.

    Static (default): the configured BASE_MARGIN_USD, derived once from BASE_CAPITAL.

    Dynamic (DYNAMIC_SIZING=true): RISK_PER_TRADE_PCT of the account's LIVE equity,
    read fresh before each open. This is the honest denominator — a hand-typed
    BASE_CAPITAL stops describing the account the moment it moves, and in a drawdown
    it errs towards MORE risk, which is the wrong direction to be wrong in.

    Returns (margin, why). margin=None means "refuse to size" and the caller must skip
    the open: with dynamic sizing on and no equity reading there is no honest number,
    and silently falling back to the static base would resurrect the very bug this
    replaces.
    """
    if not getattr(cfg, "dynamic_sizing", False):
        return cfg.base_margin_usd, "base"
    if equity is None:
        return None, "equity unreadable — refusing to size (DYNAMIC_SIZING is on)"
    floor = getattr(cfg, "equity_floor_usd", 0.0) or 0.0
    if floor > 0 and equity < floor:
        return None, f"equity {equity:.2f} below floor {floor:g} USD — not opening"
    return equity * cfg.risk_per_trade_pct / 100.0, f"{cfg.risk_per_trade_pct:g}% of {equity:.2f}"


def plan_open(coin: str, cfg: Config, signal: Optional[dict] = None,
              equity: Optional[float] = None
              ) -> "tuple[Optional[float], Optional[float], str]":
    """
    Decide (margin_usd, leverage, reason) for opening `coin`.

    `equity` is the live account value (executor.account_value()) and is only consulted
    when DYNAMIC_SIZING is on. Pass None when the account can't be read — with dynamic
    sizing that refuses the trade on purpose.

    Returns (None, None, reason) to SKIP the trade (micro-cap, zeroed multiplier, or no
    honest size available). Otherwise (margin, leverage, tier-or-mode label).
    """
    signal = signal or {}
    tier = _tier_for(coin, signal)

    # Micro-caps are never traded, in any mode.
    if tier == TIER_MICRO:
        return None, None, f"micro-cap ({coin}) — not traded"

    base, why = _base_margin(cfg, equity)
    if base is None:
        return None, None, why

    if cfg.sizing_mode == "model":
        mult = _mult_for(tier, signal)
        if mult <= 0:
            return None, None, f"zero size multiplier for {coin} (tier {tier})"
        margin = base * mult
        lev = _lev_for(tier, signal, cfg.leverage)
        return margin, lev, f"model:{tier} ({mult:g}x {why} @ {lev:g}x)"

    # fixed mode: same margin/leverage for every (non-micro) coin. Dynamic sizing still
    # applies here — "fixed" means every tier gets the same size, not that the size is
    # frozen to a number typed months ago.
    if getattr(cfg, "dynamic_sizing", False):
        return base, cfg.leverage, f"fixed ({why})"
    return cfg.size_usd, cfg.leverage, "fixed"
