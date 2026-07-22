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
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
        # Per-coin size precision (Hyperliquid `szDecimals`), lazily fetched from
        # meta() and cached. An order whose size has more decimals than the
        # asset allows is rejected as "invalid size", so this is mandatory — not
        # a nicety. None until first lookup.
        self._sz_decimals: "Optional[dict]" = None
        # Hyperliquid account abstraction mode ("unifiedAccount",
        # "portfolioMargin", ...), lazily fetched and cached on success. None
        # until known; a failed query is retried on the next read.
        self._account_mode: "Optional[str]" = None

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
            # SAFETY: make sure this API/agent wallet is actually approved to
            # trade the account we are about to MONITOR. If HL_ACCOUNT_ADDRESS is
            # not the account this wallet controls (e.g. the user pasted the API
            # wallet's OWN address instead of their main account), orders would
            # execute on a different account than the bot tracks — silently
            # disabling every risk control. Refuse to run live in that case.
            self._verify_agent_authority(wallet.address)
            self.exchange = Exchange(
                wallet,
                base_url,
                account_address=cfg.account_address,
            )
            log.info("Executor ready in LIVE mode (real orders will be placed).")
        else:
            log.info("Executor ready in DRY-RUN mode (no real orders).")

    # ------------------------------------------------------------------ #
    # Agent-authority safety check
    # ------------------------------------------------------------------ #
    def _approved_agents(self, account_address: str) -> "Optional[List[str]]":
        """
        Return the lowercased addresses of the API/agent wallets approved to
        trade `account_address` (Hyperliquid `extraAgents`). None if the query
        can't be made (no Info client or network error).
        """
        if self.info is None:
            return None
        try:
            res = self.info.post(
                "/info", {"type": "extraAgents", "user": account_address}
            )
        except Exception as exc:
            log.warning("extraAgents query failed: %s", exc)
            return None
        agents: "List[str]" = []
        for a in res or []:
            if isinstance(a, dict) and a.get("address"):
                agents.append(str(a["address"]).lower())
        return agents

    def _verify_agent_authority(self, agent_addr: str) -> None:
        """
        Ensure the API/agent wallet actually controls HL_ACCOUNT_ADDRESS.

        The wallet must appear in extraAgents(HL_ACCOUNT_ADDRESS). If it doesn't,
        orders would execute on a DIFFERENT account than the bot monitors, which
        silently disables the open-count cap, the daily-loss stop and
        reconciliation. That is more dangerous than any single bad fill, so we
        refuse to start live. Raises RuntimeError on a mismatch.
        """
        acct = (self.cfg.account_address or "").strip().lower()
        agent = (agent_addr or "").strip().lower()
        if not acct:
            raise RuntimeError("HL_ACCOUNT_ADDRESS is not set.")

        agents = self._approved_agents(self.cfg.account_address)
        if agents is not None:
            if agent not in agents:
                raise RuntimeError(
                    f"The API wallet ({agent_addr}) is NOT an approved agent of "
                    f"HL_ACCOUNT_ADDRESS ({self.cfg.account_address}). Orders would "
                    "execute on a different account than this bot monitors, "
                    "disabling every risk control. Set HL_ACCOUNT_ADDRESS to your "
                    "MAIN Hyperliquid account — the one that approved this API "
                    "wallet — NOT the API wallet's own address."
                )
            log.info(
                "Agent authority verified: API wallet is approved for %s.",
                self.cfg.account_address,
            )
            return

        # Query failed — fall back to catching the most common confusion:
        # the API wallet's OWN address pasted as the account address.
        if agent == acct:
            raise RuntimeError(
                f"HL_ACCOUNT_ADDRESS equals the API wallet's own address "
                f"({self.cfg.account_address}). It must be your MAIN Hyperliquid "
                "account address (the one that approved the API wallet)."
            )
        log.warning(
            "Could not verify the API wallet is approved for %s (network?). "
            "Proceeding — double-check that this is your MAIN account address.",
            self.cfg.account_address,
        )

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

    def position_size(self, coin: str) -> Optional[float]:
        """
        Signed size (`szi`) of the current position for `coin` on the client's
        account: >0 long, <0 short, 0.0 if flat. None if it can't be read (no Info
        client / network error / no account) — callers must treat None as "unknown"
        and NOT act, so a partial close never sizes off a guess.
        """
        if self.info is None or not self.cfg.account_address:
            return None
        try:
            user_state = self.info.user_state(self.cfg.account_address)
        except Exception as exc:
            log.warning("position_size(%s): user_state failed: %s", coin, exc)
            return None
        try:
            for ap in (user_state or {}).get("assetPositions", []) or []:
                pos = ap.get("position") if isinstance(ap, dict) else None
                if isinstance(pos, dict) and pos.get("coin") == coin:
                    return float(pos.get("szi") or 0.0)
        except (TypeError, ValueError) as exc:
            log.warning("position_size(%s): could not parse szi: %s", coin, exc)
            return None
        return 0.0

    def _abstraction_mode(self) -> "Optional[str]":
        """
        The account's Hyperliquid abstraction mode ("unifiedAccount",
        "portfolioMargin", or a classic split account). Cached after the first
        successful read — the mode only changes by explicit user action.
        Returns None if it can't be read; callers then treat the account as
        classic, which is the pre-unified behaviour.
        """
        if self._account_mode is not None:
            return self._account_mode
        if self.info is None or not self.cfg.account_address:
            return None
        try:
            mode = self.info.post(
                "/info",
                {"type": "userAbstraction", "user": self.cfg.account_address},
            )
        except Exception as exc:
            log.warning("abstraction_mode: userAbstraction query failed: %s", exc)
            return None
        if isinstance(mode, str) and mode:
            self._account_mode = mode
            return mode
        return None

    def _spot_usdc_free(self) -> Optional[float]:
        """
        Free (not held) USDC in the spot clearinghouse: total minus hold.
        Under a unified account this is the deployable capital that lives
        OUTSIDE the perps margin summary. None if it can't be read.
        """
        if self.info is None or not self.cfg.account_address:
            return None
        try:
            spot = self.info.post(
                "/info",
                {"type": "spotClearinghouseState", "user": self.cfg.account_address},
            )
            for bal in (spot or {}).get("balances", []) or []:
                if isinstance(bal, dict) and bal.get("coin") == "USDC":
                    return float(bal.get("total") or 0.0) - float(bal.get("hold") or 0.0)
            return 0.0
        except Exception as exc:
            log.warning("spot_usdc_free: spotClearinghouseState failed: %s", exc)
            return None

    def account_value(self) -> Optional[float]:
        """
        Live account equity in USD. Powers DYNAMIC_SIZING, which sizes each open
        as a percentage of this instead of a hand-typed BASE_CAPITAL.

        For a classic account this is Hyperliquid's marginSummary.accountValue: the
        margin deployed PLUS unrealized PnL. It deliberately includes unrealized PnL:
        that is what makes sizing breathe with the account — larger while positions
        are working, smaller in a drawdown. It also means the number moves with the
        market, so two signals a minute apart can size differently. That is the
        intent, not a bug.

        Under a UNIFIED account (or portfolio margin) the perps clearinghouse only
        sees the slice of USDC currently held as margin — the rest of the balance
        lives in the spot clearinghouse ("unified account and portfolio margin show
        all balances and holds in the spot clearinghouse state", HL docs). Reading
        marginSummary alone lowballs equity to roughly the margin in use, which on
        22-Jul-2026 pinned a 905-USD account at "199 USD", tripped EQUITY_FLOOR_USD
        and silently stopped every open. So here: unified equity = perps
        accountValue + free spot USDC.

        Returns None if it can't be read — including a unified account whose spot
        side can't be read: half a number is not an honest equity. The callers
        (sizing.plan_open, risk.can_open) then REFUSE to open: with no equity there
        is no honest size, and falling back to the static base would resurrect the
        exact bug dynamic sizing exists to kill.
        """
        if self.info is None or not self.cfg.account_address:
            return None
        try:
            user_state = self.info.user_state(self.cfg.account_address)
        except Exception as exc:
            log.warning("account_value: user_state failed: %s", exc)
            return None
        try:
            summary = (user_state or {}).get("marginSummary") or {}
            raw = summary.get("accountValue")
            perps = float(raw) if raw is not None else None
        except (TypeError, ValueError) as exc:
            log.warning("account_value: could not parse accountValue: %s", exc)
            return None
        if perps is None:
            return None

        mode = self._abstraction_mode()
        if mode in ("unifiedAccount", "portfolioMargin"):
            free = self._spot_usdc_free()
            if free is None:
                log.warning(
                    "account_value: %s account but spot balance unreadable — "
                    "refusing to report a lowballed equity", mode
                )
                return None
            return perps + free
        return perps

    def _sz_decimals_for(self, coin: str) -> Optional[int]:
        """
        Size precision (number of decimals) Hyperliquid allows for `coin`, from
        the perp `meta()` universe. Cached after the first successful fetch.
        Returns None if it can't be determined (caller falls back to a heuristic).
        """
        if self._sz_decimals is None and self.info is not None:
            try:
                meta = self.info.meta()
                self._sz_decimals = {
                    a["name"]: int(a["szDecimals"])
                    for a in meta.get("universe", [])
                    if isinstance(a, dict) and a.get("name") is not None
                    and a.get("szDecimals") is not None
                }
            except Exception as exc:
                log.warning("could not load szDecimals from meta: %s", exc)
                self._sz_decimals = {}   # cache the failure; retry next restart
        if not self._sz_decimals:
            return None
        return self._sz_decimals.get(coin)

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
        return self._round_size(size, coin)

    def _round_size(self, size: float, coin: Optional[str] = None) -> float:
        """
        Round order size to the coin's Hyperliquid `szDecimals`. HL rejects an
        order whose size has more decimals than the asset allows ("invalid
        size"), so we round to exactly that precision. Falls back to a
        magnitude-based heuristic only when szDecimals can't be fetched (offline
        / dry-run without an Info client).
        """
        dec = self._sz_decimals_for(coin) if coin else None
        if dec is not None:
            # Round DOWN to szDecimals so a rounding-up never nudges the notional
            # past the intended margin/leverage (and never adds a stray decimal).
            factor = 10 ** dec
            return math.floor(size * factor) / factor
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
    # Order-ack validation
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ack_ok(result: object) -> "tuple[bool, str]":
        """
        Decide whether a Hyperliquid order ack actually placed/filled.

        HL returns HTTP 200 even for BUSINESS rejections (insufficient margin,
        tick/lot violation, reduce-only, etc.) — the SDK does NOT raise in those
        cases. A rejection surfaces as a top-level status != 'ok', or a per-order
        status carrying {'error': ...}. We treat the order as accepted ONLY when
        the global status is 'ok', at least one per-order status is 'filled' or
        'resting', and NONE is an 'error'. Returns (accepted, detail).

        This is what stops a rejected order from being recorded as an open
        position locally (state divergence).
        """
        if not isinstance(result, dict):
            return False, f"no order ack ({result!r})"
        if result.get("status") != "ok":
            return False, f"exchange rejected order: {result.get('response')!r}"
        try:
            statuses = result["response"]["data"]["statuses"]
        except (KeyError, TypeError):
            return False, "order ack missing statuses"
        if not statuses:
            return False, "order ack had no statuses"
        errors = [
            s["error"]
            for s in statuses
            if isinstance(s, dict) and s.get("error")
        ]
        if errors:
            return False, "; ".join(str(e) for e in errors)
        accepted = any(
            isinstance(s, dict) and ("filled" in s or "resting" in s)
            for s in statuses
        )
        if not accepted:
            return False, f"order neither filled nor resting: {statuses!r}"
        return True, "accepted"

    # ------------------------------------------------------------------ #
    # Trading
    # ------------------------------------------------------------------ #
    def open(self, coin: str, side: str, size_usd: float, leverage: float,
             fresh: bool = True) -> dict:
        """Open a market position. side = 'long' | 'short'.

        `fresh` = this is a brand-new position (a plain open or a flip's reopen), so
        the intended margin mode MUST be confirmed before opening. Set fresh=False for
        an add/increase on an ALREADY-open position: HL locks the margin mode at the
        initial open and rejects a re-set while a position is live, which is expected.
        """
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
            # HL devuelve HTTP 200 incluso en errores de negocio y el SDK NO lanza en
            # esos casos → hay que COMPROBAR el ack. Si no se confirma y abrimos igual,
            # la posición hereda el modo por defecto de la cuenta (cross) y rompe en
            # silencio el modelo de riesgo isolated-por-trade.
            mode = "isolated" if self.cfg.isolated else "cross"
            lev_res = self.exchange.update_leverage(
                int(leverage), coin, not self.cfg.isolated
            )
            lev_ok = isinstance(lev_res, dict) and lev_res.get("status") == "ok"
            if not lev_ok:
                if fresh:
                    # Apertura nueva sin poder fijar el margen → NO abrir en el modo
                    # equivocado. Mejor no ejecutar que ejecutar en cross por error.
                    log.error(
                        "OPEN %s %s ABORTED — could not set %s margin/leverage (%r); "
                        "refusing to open in the wrong margin mode",
                        side.upper(), coin, mode, lev_res,
                    )
                    return _err(
                        f"margin-mode setup failed ({mode}): {lev_res!r}",
                        coin=coin, side=side, raw=lev_res,
                    )
                # Add/increase sobre una posición ya abierta: el modo quedó fijado en la
                # apertura inicial y HL rechaza re-fijarlo con posición viva → esperado.
                log.warning(
                    "update_leverage on %s not acked (%r) — expected for an add on an "
                    "open position; keeping the existing margin mode", coin, lev_res,
                )
            # SDK: market open with a slippage cap. market_open(coin, is_buy, sz,
            # px=None, slippage=...). slippage is a fraction (0.01 = 1%): an order
            # that can't fill within the cap is rejected instead of filling at any
            # price.
            result = self.exchange.market_open(
                coin, is_buy, size, None, self.cfg.max_slippage_pct
            )
            accepted, detail = self._ack_ok(result)
            if not accepted:
                # Rejected at the status level (no exception): do NOT record a
                # position that never opened.
                log.error(
                    "OPEN %s %s REJECTED by exchange: %s", side.upper(), coin, detail
                )
                return _err(
                    f"open rejected: {detail}", coin=coin, side=side, raw=result
                )
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
            result = self.exchange.market_close(
                coin, None, None, self.cfg.max_slippage_pct
            )
            if result is None:
                # The SDK returns None when there was NO open position for this
                # coin on the exchange. Report it honestly (no_position) so the
                # caller reconciles local state instead of assuming a clean close.
                log.warning(
                    "CLOSE %s: no open position on exchange (nothing to close)", coin
                )
                return _ok(
                    "close: no open position",
                    coin=coin,
                    realized=None,
                    no_position=True,
                )
            accepted, detail = self._ack_ok(result)
            if not accepted:
                log.error("CLOSE %s REJECTED by exchange: %s", coin, detail)
                return _err(f"close rejected: {detail}", coin=coin, raw=result)
            # Read the realized PnL of this close so the daily-loss stop can see
            # it. Never lets a PnL-lookup problem abort the close itself.
            realized = self._realized_pnl_for_oids(self._extract_oids(result))
            if realized is not None:
                log.info("CLOSE %s -> realized %.2f USD", coin, realized)
            else:
                log.warning(
                    "CLOSE %s: could not read realized PnL — daily-loss counter "
                    "NOT updated for this close (corrected at next reconcile)",
                    coin,
                )
            log.info("CLOSE %s -> %s", coin, result)
            return _ok("live close", raw=result, coin=coin, realized=realized)
        except Exception as exc:
            log.error("close(%s) failed: %s", coin, exc)
            return _err(f"close failed: {exc}", coin=coin)

    def reduce(self, coin: str, fraction: float) -> dict:
        """
        Reduce-only: close `fraction` (0..1) of the CURRENT position for `coin`,
        leaving the rest open. Used to mirror a model `decrease` (one of N copies
        closed) proportionally, e.g. 3->2 copies trims 1/3.

        The size is taken from the REAL `szi` on the exchange at call time (not a
        stored value), so it stays correct even if the client changed their size
        mid-position. Uses market_close with an explicit size, which HL treats as
        reduce-only (it can only shrink, never flip, the position).
        """
        fraction = max(0.0, min(1.0, float(fraction)))
        if fraction <= 0.0:
            return _ok(f"reduce {coin} by 0% (noop)", coin=coin, realized=None)
        # A full (or near-full) trim is just a close — avoid leaving dust behind.
        if fraction >= 0.999:
            return self.close(coin)

        if not self.live:
            log.info("[DRY-RUN] REDUCE %s by %.1f%% (reduce-only)", coin, fraction * 100)
            return _ok(
                f"dry-run reduce {coin} {fraction:.0%}",
                coin=coin, fraction=fraction, realized=None,
            )

        szi = self.position_size(coin)
        if szi is None:
            # Unknown real size — do NOT guess. Report so the caller keeps its
            # units untouched and a later reconcile/decrease corrects things.
            return _err("reduce: could not read position size", coin=coin)
        if szi == 0.0:
            log.warning("REDUCE %s: no open position on exchange (nothing to trim)", coin)
            return _ok("reduce: no open position", coin=coin, no_position=True, realized=None)

        size = self._round_size(abs(szi) * fraction, coin)
        if size <= 0:
            # The proportional slice rounds below the asset's min size — skip rather
            # than accidentally closing the whole thing.
            log.warning(
                "REDUCE %s by %.1f%% rounds to size 0 (szi=%s) — skipping trim",
                coin, fraction * 100, szi,
            )
            return _err(f"reduce size rounds to 0 for {coin}", coin=coin)

        try:
            # market_close with an explicit sz reduces only that many coins (reduce-only).
            result = self.exchange.market_close(
                coin, size, None, self.cfg.max_slippage_pct
            )
            if result is None:
                log.warning("REDUCE %s: no open position on exchange", coin)
                return _ok("reduce: no open position", coin=coin, no_position=True, realized=None)
            accepted, detail = self._ack_ok(result)
            if not accepted:
                log.error("REDUCE %s REJECTED by exchange: %s", coin, detail)
                return _err(f"reduce rejected: {detail}", coin=coin, raw=result)
            realized = self._realized_pnl_for_oids(self._extract_oids(result))
            log.info(
                "REDUCE %s by %.1f%% (sz=%s) -> realized %s",
                coin, fraction * 100, size,
                f"{realized:.2f} USD" if realized is not None else "undetermined",
            )
            return _ok(
                "live reduce", raw=result, coin=coin, size=size,
                fraction=fraction, realized=realized,
            )
        except Exception as exc:
            log.error("reduce(%s, %.3f) failed: %s", coin, fraction, exc)
            return _err(f"reduce failed: {exc}", coin=coin)

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
                closed=True,
                reopened=True,
            )

        close_res = self.close(coin)
        if not close_res.get("ok"):
            # A real close FAILURE (rejected/exception) — the old position may
            # still be open. Abort without touching state (closed=False).
            return _err(
                f"flip aborted, close failed: {close_res.get('detail')}",
                coin=coin,
                closed=False,
                close=close_res,
            )
        # The close leg succeeded (or there was nothing to close): carry its
        # realized PnL so the caller can feed the daily-loss counter.
        realized = close_res.get("realized")
        open_res = self.open(coin, new_side, size_usd, leverage)
        if not open_res.get("ok"):
            # Closed but could NOT reopen: the account is now FLAT. Report
            # closed=True / reopened=False so the caller records a close, never
            # a phantom open on the new side.
            log.error(
                "FLIP %s: closed but reopen failed: %s", coin, open_res.get("detail")
            )
            return _err(
                f"flip: closed but reopen failed: {open_res.get('detail')}",
                coin=coin,
                closed=True,
                reopened=False,
                realized=realized,
                close=close_res,
                open=open_res,
            )
        return _ok(
            f"flip {coin} to {new_side}",
            coin=coin,
            side=new_side,
            closed=True,
            reopened=True,
            realized=realized,
            close=close_res,
            open=open_res,
        )

    # ------------------------------------------------------------------ #
    # Reconciliation — HL account is the source of truth
    # ------------------------------------------------------------------ #
    def reconcile(self) -> "Optional[Dict[str, object]]":
        """
        Read the client's REAL state from Hyperliquid and return the truth:

            {"positions": {coin: "long"|"short"}, "realized_today": float|None}

        `positions` come from clearinghouseState assetPositions (sign of szi).
        `realized_today` sums closedPnl over today's (UTC) fills — authoritative
        even when an individual close's PnL read failed, which is what makes the
        daily-loss stop reliable. Returns None if HL is unreachable (the caller
        then keeps its current view untouched).
        """
        if self.info is None or not self.cfg.account_address:
            return None
        try:
            user_state = self.info.user_state(self.cfg.account_address)
        except Exception as exc:
            log.warning("reconcile: user_state failed: %s", exc)
            return None

        positions: "Dict[str, str]" = {}
        try:
            for ap in (user_state or {}).get("assetPositions", []) or []:
                pos = ap.get("position") if isinstance(ap, dict) else None
                if not isinstance(pos, dict):
                    continue
                coin = pos.get("coin")
                try:
                    szi = float(pos.get("szi") or 0.0)
                except (TypeError, ValueError):
                    szi = 0.0
                if coin and szi != 0.0:
                    positions[coin] = "long" if szi > 0 else "short"
        except Exception as exc:
            log.warning("reconcile: could not parse positions: %s", exc)
            return None

        realized_today = self._realized_today_from_fills()
        return {"positions": positions, "realized_today": realized_today}

    def _realized_today_from_fills(self) -> Optional[float]:
        """
        Sum closedPnl across the client's fills from the start of the current UTC
        day. Source of truth for the daily-loss stop. None on any failure (the
        caller then leaves its counter as-is rather than zeroing it).
        """
        if self.info is None or not self.cfg.account_address:
            return None
        now = datetime.now(timezone.utc)
        day_start_ms = int(
            datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
            * 1000
        )
        try:
            fills = self.info.user_fills(self.cfg.account_address)
        except Exception as exc:
            log.warning("reconcile: user_fills failed: %s", exc)
            return None
        total = 0.0
        for f in fills or []:
            if not isinstance(f, dict):
                continue
            t = f.get("time")
            try:
                if t is not None and int(t) < day_start_ms:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                total += float(f.get("closedPnl") or 0.0)
            except (TypeError, ValueError):
                pass
        return total
