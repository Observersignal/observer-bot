"""
bot.py — The Observer Auto-Execution Bot (entry point).

Non-custodial. Runs on the client's own machine with the client's own keys.
Polls the signal feed and mirrors open / close / flip events onto the client's
own Hyperliquid account. Defaults to DRY-RUN: nothing trades until the client
explicitly sets DRY_RUN=false in their .env.

    python bot.py        # headless: run the loop until Ctrl-C (VPS / 24-7)
    python app.py        # local web UI to configure + Start/Stop the loop

Stop with Ctrl-C (state is saved). Kill-switch: `touch STOP` to stop opening
new trades without killing the process.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Callable, Optional

import config
import feed
import risk
import state as state_mod
from executor import Executor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("observer.bot")


def _banner(cfg: config.Config) -> None:
    mode = "LIVE  (real orders)" if cfg.configured_live() else "DRY-RUN  (simulate only)"
    if cfg.mock_mode:
        mode += "  +  DEV MOCK FEED"
    line = "=" * 64
    log.info(line)
    log.info("  The Observer — Auto-Execution Bot")
    log.info("  Non-custodial: your keys, your account, your risk.")
    log.info("  MODE: %s", mode)
    log.info(line)
    for row in cfg.summary().splitlines():
        log.info(row)
    log.info(line)
    if not cfg.configured_live():
        log.info(
            "No real orders will be placed. To go live: set real keys in .env "
            "and DRY_RUN=false."
        )
    log.info("Kill-switch: create a file named 'STOP' to halt new opens.")
    log.info(line)


def _apply_realized(res: dict, st: state_mod.State) -> None:
    """
    Feed the realized PnL of a close into the daily-loss counter.

    Live closes carry a `realized` field (USD, negative when losing); a flip
    carries it nested under `close`. Dry-run and any close where PnL couldn't
    be read leave it as None — we simply don't touch the counter then. This is
    what makes the daily-loss stop in risk.can_open() actually fire.
    """
    if not isinstance(res, dict):
        return
    realized = res.get("realized")
    if realized is None and isinstance(res.get("close"), dict):
        realized = res["close"].get("realized")
    if realized is not None:
        st.add_realized(realized)


def _handle_event(
    ev: dict, st: state_mod.State, ex: Executor, cfg: config.Config
) -> None:
    """Route a single signal to the executor and update local state."""
    event = ev["event"]
    coin = ev["coin"]
    side = ev["side"]

    # Staleness guard. If the bot was offline and reconnects, an OPEN signal
    # may be far older than the live entry — chasing a moved entry is risky.
    # CLOSE always runs regardless of age (a late close is safer than an open
    # position left dangling). Age is in minutes for human-readable logs.
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - int(ev.get("ts") or now_ms)
    stale = age_ms > cfg.max_signal_age_min * 60_000
    age_min = age_ms / 60_000.0

    if event == "open":
        if stale:
            log.warning(
                "skip OPEN %s %s — signal too old (%.0f min), entry already moved",
                side,
                coin,
                age_min,
            )
            return
        allowed, reason = risk.can_open(st, cfg)
        if not allowed:
            log.warning("skip OPEN %s %s — blocked: %s", side, coin, reason)
            return
        res = ex.open(coin, side, cfg.size_usd, cfg.leverage)
        if res.get("ok"):
            st.record_open(coin, side)

    elif event == "close":
        # ALWAYS execute, regardless of age — closing late beats not closing.
        res = ex.close(coin)
        if res.get("ok"):
            st.record_close(coin)
            _apply_realized(res, st)

    elif event == "flip":
        if not cfg.allow_flip:
            log.info("skip FLIP %s — ALLOW_FLIP is false", coin)
            return
        if stale:
            # A late close is good; a late re-entry is not. Close only.
            log.warning(
                "FLIP %s stale (%.0f min) — closing only, not reopening",
                coin,
                age_min,
            )
            res = ex.close(coin)
            if res.get("ok"):
                st.record_close(coin)
                _apply_realized(res, st)
            return
        # A flip opens a new position; honour the open-side risk checks.
        allowed, reason = risk.can_open(st, cfg)
        if not allowed:
            # Still close the existing side to reduce risk, but don't reopen.
            log.warning(
                "FLIP %s blocked from reopening (%s); closing only", coin, reason
            )
            res = ex.close(coin)
            if res.get("ok"):
                st.record_close(coin)
                _apply_realized(res, st)
            return
        res = ex.flip(coin, side, cfg.size_usd, cfg.leverage)
        if res.get("ok"):
            _apply_realized(res, st)  # realized comes from the close leg
            st.record_open(coin, side)

    elif event == "increase":
        if not cfg.allow_increase:
            log.info("skip INCREASE %s — ALLOW_INCREASE is false", coin)
            return
        if stale:
            log.warning(
                "skip INCREASE %s %s — signal too old (%.0f min), entry already moved",
                side,
                coin,
                age_min,
            )
            return
        allowed, reason = risk.can_open(st, cfg)
        if not allowed:
            log.warning("skip INCREASE %s %s — blocked: %s", side, coin, reason)
            return
        res = ex.open(coin, side, cfg.size_usd, cfg.leverage)
        if res.get("ok"):
            st.record_open(coin, side)

    else:
        log.warning("unknown event type '%s' — ignoring", event)


class _SinkHandler(logging.Handler):
    """A logging handler that forwards formatted records to a callback.

    Used by the UI to mirror the bot's log lines into a ring buffer it can
    show. Any exception in the sink is swallowed so logging never breaks the
    loop.
    """

    def __init__(self, sink: "Callable[[str], None]"):
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._sink(self.format(record))
        except Exception:
            pass


def run_loop(
    stop_event: threading.Event,
    log_sink: "Optional[Callable[[str], None]]" = None,
) -> int:
    """
    Run the poll/execute loop until `stop_event` is set.

    This is the reusable core shared by the headless `bot.py` entry point and
    the web UI (`app.py`). It:
      - reads a FRESH config snapshot (so a UI config-save takes effect on the
        next Start),
      - optionally mirrors every log line to `log_sink` (a callback taking a
        single formatted string) in addition to the normal logger, and
      - loops on `stop_event.wait(poll_seconds)` so a Stop is responsive.

    Returns 0 on a clean shutdown.
    """
    # Snapshot config for this run. config.CFG is rebuilt by config.reload()
    # after a save, so each run_loop call sees the latest values.
    cfg = config.CFG

    sink_handler: "Optional[_SinkHandler]" = None
    if log_sink is not None:
        sink_handler = _SinkHandler(log_sink)
        sink_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S")
        )
        # Attach to the package root so all observer.* loggers feed the sink.
        logging.getLogger("observer").addHandler(sink_handler)

    try:
        _banner(cfg)

        st = state_mod.load()
        log.info(
            "loaded state: %s open position(s), cursor=%r, realized today=%.2f USD",
            st.count_open(),
            st.cursor,
            st.realized_today_usd,
        )

        try:
            ex = Executor(cfg)
        except Exception as exc:
            log.error("could not start executor: %s", exc)
            return 1

        # First-run BASELINE: on a brand-new install the cursor is empty, which
        # against the real feed (since=0) returns the ENTIRE signal history. We
        # must NEVER replay that history as live trades. The FIRST SUCCESSFUL poll
        # in the loop adopts its cursor WITHOUT acting; only signals after it are
        # traded. Done inside the loop (via this flag) so a failed first poll just
        # retries on the next iteration — there is no window for a history replay.
        # (Skipped in DEV MOCK MODE — the demo is meant to replay its script.)
        need_baseline = (not cfg.mock_mode and not st.cursor)

        log.info(
            "entering main loop (poll every %ss). %s",
            cfg.poll_seconds,
            "Ctrl-C to stop." if log_sink is None else "Stop from the UI to halt.",
        )

        while not stop_event.is_set():
            try:
                events, new_cursor = feed.poll(st.cursor)

                if need_baseline:
                    # Primera lectura correcta = baseline: adoptamos el cursor SIN
                    # actuar sobre el histórico. Si el poll falla, seguimos en modo
                    # baseline y se reintenta — nunca se reproduce el histórico.
                    st.cursor = new_cursor
                    st.save()
                    need_baseline = False
                    log.info(
                        "Baseline set (cursor=%r). Acting only on NEW signals from here.",
                        st.cursor,
                    )
                else:
                    for ev in events:
                        sid = ev["id"]
                        if st.seen(sid):
                            continue
                        try:
                            _handle_event(ev, st, ex, cfg)
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

            # Interruptible sleep: returns immediately when stop_event is set.
            stop_event.wait(cfg.poll_seconds)

        st.save()
        log.info("state saved. goodbye.")
        return 0
    finally:
        if sink_handler is not None:
            logging.getLogger("observer").removeHandler(sink_handler)


def main() -> int:
    """Headless entry point: run until Ctrl-C / SIGTERM (VPS, 24-7)."""
    stop_event = threading.Event()

    def _handle_sigint(signum, frame):  # noqa: ARG001
        log.info("shutdown requested — finishing up and saving state...")
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    return run_loop(stop_event)


if __name__ == "__main__":
    sys.exit(main())
