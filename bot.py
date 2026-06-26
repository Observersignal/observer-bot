"""
bot.py — The Observer Auto-Execution Bot (entry point).

Non-custodial. Runs on the client's own machine with the client's own keys.
Polls the signal feed and mirrors open / close / flip events onto the client's
own Hyperliquid account. Defaults to DRY-RUN: nothing trades until the client
explicitly sets DRY_RUN=false in their .env.

    python bot.py        # with no config -> DEV MOCK + DRY-RUN demo

Stop with Ctrl-C (state is saved). Kill-switch: `touch STOP` to stop opening
new trades without killing the process.
"""

from __future__ import annotations

import logging
import signal
import sys
import time

import feed
import risk
import state as state_mod
from config import CFG
from executor import Executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("observer.bot")

# Set by the signal handler to ask the main loop to wind down cleanly.
_stop_requested = False


def _handle_sigint(signum, frame):  # noqa: ARG001
    global _stop_requested
    _stop_requested = True
    log.info("shutdown requested — finishing up and saving state...")


def _banner() -> None:
    mode = "LIVE  (real orders)" if CFG.configured_live() else "DRY-RUN  (simulate only)"
    if CFG.mock_mode:
        mode += "  +  DEV MOCK FEED"
    line = "=" * 64
    log.info(line)
    log.info("  The Observer — Auto-Execution Bot")
    log.info("  Non-custodial: your keys, your account, your risk.")
    log.info("  MODE: %s", mode)
    log.info(line)
    for row in CFG.summary().splitlines():
        log.info(row)
    log.info(line)
    if not CFG.configured_live():
        log.info(
            "No real orders will be placed. To go live: set real keys in .env "
            "and DRY_RUN=false."
        )
    log.info("Kill-switch: create a file named 'STOP' to halt new opens.")
    log.info(line)


def _handle_event(ev: dict, st: state_mod.State, ex: Executor) -> None:
    """Route a single signal to the executor and update local state."""
    event = ev["event"]
    coin = ev["coin"]
    side = ev["side"]

    if event == "open":
        allowed, reason = risk.can_open(st, CFG)
        if not allowed:
            log.warning("skip OPEN %s %s — blocked: %s", side, coin, reason)
            return
        res = ex.open(coin, side, CFG.size_usd, CFG.leverage)
        if res.get("ok"):
            st.record_open(coin, side)

    elif event == "close":
        res = ex.close(coin)
        if res.get("ok"):
            st.record_close(coin)

    elif event == "flip":
        if not CFG.allow_flip:
            log.info("skip FLIP %s — ALLOW_FLIP is false", coin)
            return
        # A flip opens a new position; honour the open-side risk checks.
        allowed, reason = risk.can_open(st, CFG)
        if not allowed:
            # Still close the existing side to reduce risk, but don't reopen.
            log.warning(
                "FLIP %s blocked from reopening (%s); closing only", coin, reason
            )
            res = ex.close(coin)
            if res.get("ok"):
                st.record_close(coin)
            return
        res = ex.flip(coin, side, CFG.size_usd, CFG.leverage)
        if res.get("ok"):
            st.record_open(coin, side)

    elif event == "increase":
        if not CFG.allow_increase:
            log.info("skip INCREASE %s — ALLOW_INCREASE is false", coin)
            return
        allowed, reason = risk.can_open(st, CFG)
        if not allowed:
            log.warning("skip INCREASE %s %s — blocked: %s", side, coin, reason)
            return
        res = ex.open(coin, side, CFG.size_usd, CFG.leverage)
        if res.get("ok"):
            st.record_open(coin, side)

    else:
        log.warning("unknown event type '%s' — ignoring", event)


def main() -> int:
    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    _banner()

    st = state_mod.load()
    log.info(
        "loaded state: %s open position(s), cursor=%r, realized today=%.2f USD",
        st.count_open(),
        st.cursor,
        st.realized_today_usd,
    )

    try:
        ex = Executor(CFG)
    except Exception as exc:
        log.error("could not start executor: %s", exc)
        return 1

    log.info("entering main loop (poll every %ss). Ctrl-C to stop.", CFG.poll_seconds)

    while not _stop_requested:
        try:
            events, new_cursor = feed.poll(st.cursor)

            for ev in events:
                sid = ev["id"]
                if st.seen(sid):
                    continue
                try:
                    _handle_event(ev, st, ex)
                except Exception as exc:
                    # One bad signal must never kill the loop.
                    log.error("error handling signal %s: %s", sid, exc)
                finally:
                    st.mark_processed(sid)

            st.cursor = new_cursor
            st.save()

        except Exception as exc:
            # Defensive catch-all so the loop survives any unexpected error.
            log.error("loop iteration error: %s", exc)

        # Interruptible sleep so Ctrl-C is responsive.
        slept = 0.0
        while slept < CFG.poll_seconds and not _stop_requested:
            time.sleep(0.25)
            slept += 0.25

    st.save()
    log.info("state saved. goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
