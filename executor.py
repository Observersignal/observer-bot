"""
executor.py — Hyperliquid adapter (trade-only).

Places orders on the CLIENT'S OWN Hyperliquid account using the client's own
API/agent wallet key. There is NO withdrawal, transfer or fund-movement code
anywhere in this file — by design. The key the client provides is a trade-only
agent wallet that cannot move funds off the exchange.

DRY_RUN is the safe default: in dry-run mode no SDK call is ever made; the
adapter only logs exactly what it WOULD do and returns a fake-ok result.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from config import Config

log = logging.getLogger("observer.exec")

# --- Hyperliquid SDK imports ----------------------------------------------
# Imported lazily-tolerant: a fresh dev box can run DEV MOCK + DRY-RUN without
# the SDK fully wired. If live trading is requested the constructor will raise
# clearly if the SDK is missing.
try:
    from hyperliquid.exchange import Exchange  # type: ignore
    from hyperliquid.info import Info  # type: ignore
    from eth_account import Account  # type: ignore

    _SDK_OK = True
except Exception as _imp_exc:  # pragma: no cover - environment dependent
    Exchange = None  # type: ignore
    Info = None  # type: ignore
    Account = None  # type: ignore
    _SDK_OK = False
    _SDK_IMPORT_ERROR = _imp_exc

# Mainnet API base URL. Prefer the SDK constant; fall back to the literal.
try:
    from hyperliquid.utils.constants import MAINNET_API_URL  # type: ignore
except Exception:  # pragma: no cover
    MAINNET_API_URL = "https://api.hyperliquid.xyz"


# Indicative placeholder prices, used ONLY in dry-run when no live price is
# available (e.g. the SDK is not installed yet), so the simulation still shows
# a realistic size. Never used for real orders.
_DRYRUN_FALLBACK_PRICE = {
    "BTC": 65000.0,
    "ETH": 3500.0,
    "SOL": 150.0,
}
_DRYRUN_DEFAULT_PRICE = 100.0


def _ok(detail: str, **extra) -> dict:
    return {"ok": True, "detail": detail, **extra}


def _err(detail: str, **extra) -> dict:
    return {"ok": False, "detail": detail, **extra}


class Executor:
    """Thin wrapper over the Hyperliquid SDK with a hard dry-run gate."""

    def __init__(self, cfg: Config, base_url: str = MAINNET_API_URL):
        self.cfg = cfg
        self.base_url = base_url
        self.live = cfg.configured_live()

        self.info = None
        self.exchange = None

        # Info (public market data) is always useful; build it if we can.
        if _SDK_OK and Info is not None:
            try:
                self.info = Info(base_url, skip_ws=True)
            except Exception as exc:
                log.warning("could not init Info client: %s", exc)
                self.info = None

        if self.live:
            if not _SDK_OK:
                raise RuntimeError(
                    "Live trading requested but the Hyperliquid SDK is not "
                    f"installed: {_SDK_IMPORT_ERROR!r}. Run "
                    "`pip install -r requirements.txt`."
                )
            # Build the authenticated Exchange client from the client's
            # trade-only agent wallet key.
            wallet = Account.from_key(cfg.api_secret)
            self.exchange = Exchange(
                wallet,
                base_url,
                account_address=cfg.account_address,
            )
            log.info("Executor ready in LIVE mode (real orders will be placed).")
        else:
            log.info("Executor ready in DRY-RUN mode (no real orders).")

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #
    def mark_price(self, coin: str) -> Optional[float]:
        """Current mark price for `coin` via Info all-mids. None on failure."""
        if self.info is None:
            return None
        try:
            # SDK: Info.all_mids() -> {"BTC": "65000.0", "ETH": "3500.0", ...}
            mids = self.info.all_mids()
            raw = mids.get(coin)
            if raw is None:
                log.warning("no mark price for %s in all-mids", coin)
                return None
            return float(raw)
        except Exception as exc:
            log.warning("mark_price(%s) failed: %s", coin, exc)
            return None

    def _price_or_fallback(self, coin: str) -> Optional[float]:
        """
        Live mark price, or — in DRY-RUN only — an indicative placeholder so
        the simulation can still show a size. In LIVE mode returns None when no
        real price is available, which correctly blocks the order.
        """
        price = self.mark_price(coin)
        if price and price > 0:
            return price
        if not self.live:
            return _DRYRUN_FALLBACK_PRICE.get(coin, _DRYRUN_DEFAULT_PRICE)
        return None

    def size_for(self, coin: str, size_usd: float, leverage: float) -> float:
        """
        Coin-denominated order size from desired USD margin and leverage:
            notional = size_usd * leverage
            size     = notional / mark_price
        Rounded to a sensible precision. Returns 0.0 if price unavailable.
        """
        price = self._price_or_fallback(coin)
        if not price or price <= 0:
            return 0.0
        notional = size_usd * leverage
        size = notional / price
        return self._round_size(size)

    @staticmethod
    def _round_size(size: float) -> float:
        """Round order size to a precision that suits the coin's price range."""
        if size >= 1000:
            return round(size, 1)
        if size >= 1:
            return round(size, 3)
        return round(size, 4)

    # ------------------------------------------------------------------ #
    # Realized PnL (feeds the daily-loss stop)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_oids(result: dict) -> "List[int]":
        """
        Pull the order ids of the fills out of a Hyperliquid order response.

        Shape: result["response"]["data"]["statuses"] is a list of dicts, each
        either {"filled": {"oid": .., "totalSz": .., "avgPx": ..}} or
        {"resting": {...}} / {"error": ".."}. We only care about filled ones.
        Any shape surprise just yields no oids (PnL stays undetermined) rather
        than raising.
        """
        oids: "List[int]" = []
        try:
            statuses = result["response"]["data"]["statuses"]
            for s in statuses:
                filled = s.get("filled") if isinstance(s, dict) else None
                if filled and filled.get("oid") is not None:
                    oids.append(int(filled["oid"]))
        except Exception:
            pass
        return oids

    def _realized_pnl_for_oids(
        self, oids: "List[int]", retries: int = 3, delay: float = 0.4
    ) -> Optional[float]:
        """
        Sum the realized (closed) PnL of the fills belonging to `oids`.

        Queries the client's own fills via the Info API and sums `closedPnl`
        over every fill whose oid we placed. A single close can produce several
        partial fills sharing one oid — summing matching-oid fills captures all
        of them. Matching by oid (not by time) means restarts and concurrent
        closes never double-count.

        Fills can lag the order ack by a beat, so we retry briefly until all
        requested oids are visible. Returns None on any failure or if no fills
        ever appear — the caller treats None as "don't touch the counter" and
        logs that the daily stop wasn't fed.
        """
        if not oids or self.info is None or not self.cfg.account_address:
            return None
        want = {int(o) for o in oids}
        for attempt in range(retries):
            try:
                fills = self.info.user_fills(self.cfg.account_address)
            except Exception as exc:
                log.warning("user_fills failed while pricing realized PnL: %s", exc)
                return None
            total = 0.0
            found: "set[int]" = set()
            for f in fills or []:
                oid = f.get("oid") if isinstance(f, dict) else None
                if oid is None or int(oid) not in want:
                    continue
                found.add(int(oid))
                try:
                    total += float(f.get("closedPnl") or 0.0)
                except (TypeError, ValueError):
                    pass
            if found and (found >= want or attempt == retries - 1):
                return total
            time.sleep(delay)
        return None

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #
    def open(self, coin: str, side: str, size_usd: float, leverage: float) -> dict:
        """Open a market position. side = 'long' | 'short'."""
        is_buy = side == "long"
        size = self.size_for(coin, size_usd, leverage)
        price = self._price_or_fallback(coin)

        if size <= 0:
            return _err(f"computed size 0 for {coin} (no price?)")

        if not self.live:
            log.info(
                "[DRY-RUN] OPEN %s %s | size=%s | margin=%sUSD @ %sx | "
                "mark=%s",
                side.upper(),
                coin,
                size,
                f"{size_usd:g}",
                f"{leverage:g}",
                price,
            )
            return _ok(
                f"dry-run open {side} {coin}",
                coin=coin,
                side=side,
                size=size,
                leverage=leverage,
                price=price,
            )

        try:
            # SDK: leverage + modo de margen. update_leverage(leverage, coin, is_cross).
            # is_cross=False -> ISOLATED (lo que sigue el modelo); True -> cross.
            self.exchange.update_leverage(int(leverage), coin, not self.cfg.isolated)
            # SDK: market open. market_open(coin, is_buy, sz, px=None, slippage=...)
            result = self.exchange.market_open(coin, is_buy, size)
            log.info("OPEN %s %s size=%s -> %s", side.upper(), coin, size, result)
            return _ok("live open", raw=result, coin=coin, side=side, size=size)
        except Exception as exc:
            log.error("open(%s, %s) failed: %s", coin, side, exc)
            return _err(f"open failed: {exc}", coin=coin, side=side)

    def close(self, coin: str) -> dict:
        """Market-close the entire position for `coin`."""
        price = self._price_or_fallback(coin)

        if not self.live:
            log.info("[DRY-RUN] CLOSE %s | mark=%s", coin, price)
            return _ok(f"dry-run close {coin}", coin=coin, price=price)

        try:
            # SDK: market close the whole position for this coin.
            # market_close(coin, sz=None, px=None, slippage=...) — sz=None = full.
            result = self.exchange.market_close(coin)
            # Read the realized PnL of this close so the daily-loss stop can see
            # it. Never lets a PnL-lookup problem abort the close itself.
            realized = self._realized_pnl_for_oids(self._extract_oids(result))
            if realized is not None:
                log.info("CLOSE %s -> realized %.2f USD", coin, realized)
            else:
                log.warning(
                    "CLOSE %s: could not read realized PnL — daily-loss counter "
                    "NOT updated for this close",
                    coin,
                )
            log.info("CLOSE %s -> %s", coin, result)
            return _ok("live close", raw=result, coin=coin, realized=realized)
        except Exception as exc:
            log.error("close(%s) failed: %s", coin, exc)
            return _err(f"close failed: {exc}", coin=coin)

    def flip(
        self, coin: str, new_side: str, size_usd: float, leverage: float
    ) -> dict:
        """Flip: close the current position, then open the opposite side."""
        if not self.live:
            price = self._price_or_fallback(coin)
            size = self.size_for(coin, size_usd, leverage)
            log.info(
                "[DRY-RUN] FLIP %s -> %s | close then open | new size=%s | "
                "margin=%sUSD @ %sx | mark=%s",
                coin,
                new_side.upper(),
                size,
                f"{size_usd:g}",
                f"{leverage:g}",
                price,
            )
            return _ok(
                f"dry-run flip {coin} to {new_side}",
                coin=coin,
                side=new_side,
                size=size,
                price=price,
            )

        close_res = self.close(coin)
        if not close_res.get("ok"):
            return _err(f"flip aborted, close failed: {close_res.get('detail')}")
        open_res = self.open(coin, new_side, size_usd, leverage)
        return _ok(
            f"flip {coin} to {new_side}",
            close=close_res,
            open=open_res,
            coin=coin,
            side=new_side,
        )
