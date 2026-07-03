"""
feed.py — read signals from The Observer's authenticated feed.

This client only CONSUMES signals. It knows nothing about how a signal is
produced; from its point of view a signal is just:

    {"id": str, "event": "open"|"close"|"flip"|"increase",
     "coin": str, "side": "long"|"short", "ts": int}

NOTE: the real `/api/signals/feed` endpoint is built separately on the
server (phase F1). This client just polls it. If no feed token is set, the
bot runs in DEV MOCK MODE and replays a short scripted sequence locally so
the mechanics can be watched end-to-end in DRY-RUN without the feed existing.
"""

from __future__ import annotations

import logging
import time
from typing import List, Tuple

import requests

import config

log = logging.getLogger("observer.feed")

# How long to wait on the HTTP request before giving up (seconds).
_HTTP_TIMEOUT = 10

# ---------------------------------------------------------------------------
# DEV mock sequence — replayed one signal per poll when there is no token.
# Lets a fresh install watch open / close / flip mechanics immediately.
# ---------------------------------------------------------------------------
_MOCK_SCRIPT = [
    {"id": "mock-1", "event": "open", "coin": "BTC", "side": "long", "ts": 1},
    {"id": "mock-2", "event": "open", "coin": "ETH", "side": "short", "ts": 2},
    {"id": "mock-3", "event": "close", "coin": "BTC", "side": "long", "ts": 3},
    {"id": "mock-4", "event": "flip", "coin": "ETH", "side": "long", "ts": 4},
]

# Track how far through the mock script we have walked (index into _MOCK_SCRIPT).
_mock_pos = 0


def _poll_mock(cursor: str) -> Tuple[List[dict], str]:
    """Yield the next scripted mock signal (one per poll), then go quiet."""
    global _mock_pos
    if _mock_pos >= len(_MOCK_SCRIPT):
        # Sequence exhausted: nothing more to emit. The bot keeps idling.
        return [], cursor
    # Emit a COPY with a FRESH timestamp so the demo signal is never "stale"
    # under the staleness guard (the script's tiny fixed ts would otherwise be
    # treated as ancient and OPEN signals would be skipped). Don't mutate the
    # module-level script.
    sig = dict(_MOCK_SCRIPT[_mock_pos])
    sig["ts"] = int(time.time() * 1000)
    _mock_pos += 1
    log.info(
        "DEV MOCK MODE — emitting scripted signal %s/%s: %s %s %s",
        _mock_pos,
        len(_MOCK_SCRIPT),
        sig["event"],
        sig["side"],
        sig["coin"],
    )
    return [sig], sig["id"]


def _valid_signal(s: object) -> bool:
    """Defensive shape check so a malformed payload never reaches the executor."""
    if not isinstance(s, dict):
        return False
    if not isinstance(s.get("id"), str) or not s["id"]:
        return False
    if s.get("event") not in ("open", "close", "flip", "increase"):
        return False
    if not isinstance(s.get("coin"), str) or not s["coin"]:
        return False
    if s.get("side") not in ("long", "short"):
        return False
    return True


def _new_cursor(cursor: str, events: List[dict]) -> str:
    """Advance the cursor to the largest id/ts we have seen."""
    new = cursor
    for e in events:
        candidate = str(e.get("id") or e.get("ts") or "")
        if candidate and candidate > new:
            new = candidate
    return new


def poll(cursor: str) -> Tuple[List[dict], str]:
    """
    Poll the feed once.

    Returns (events, new_cursor). On any error returns ([], cursor) so the
    main loop simply tries again on the next tick — it never raises.
    """
    # Read the live config at call-time so a config reload (from the UI) is
    # picked up without re-importing this module.
    cfg = config.CFG

    if cfg.mock_mode:
        return _poll_mock(cursor)

    url = f"{cfg.api_url.rstrip('/')}/api/signals/feed"
    headers = {
        "Authorization": f"Bearer {cfg.feed_token}",
        "X-Feed-Token": cfg.feed_token,
        "Accept": "application/json",
    }
    params = {"since": cursor}

    try:
        resp = requests.get(
            url, headers=headers, params=params, timeout=_HTTP_TIMEOUT
        )
    except requests.RequestException as exc:
        log.warning("feed request failed (%s); will retry next tick", exc)
        return [], cursor

    if resp.status_code in (401, 403):
        # An expired or rejected token — NOT a transient blip. Feed tokens
        # expire (~35 days); this is almost always "your token lapsed". Say so
        # clearly and loudly so the client renews instead of silently running
        # blind for days. Still returns [] so the loop keeps running (a renewed
        # token in .env is picked up on the next Start).
        log.error(
            "feed rejected the token (HTTP %s) — it looks EXPIRED or invalid. "
            "Send /bot to The Observer Telegram bot for a fresh token, then "
            "update OBSERVER_FEED_TOKEN in your .env and restart. No new signals "
            "until then.",
            resp.status_code,
        )
        return [], cursor

    if resp.status_code != 200:
        log.warning(
            "feed returned HTTP %s; will retry next tick", resp.status_code
        )
        return [], cursor

    try:
        payload = resp.json()
    except ValueError:
        log.warning("feed returned non-JSON body; will retry next tick")
        return [], cursor

    # Accept either a bare list or {"signals": [...], "cursor": "..."}.
    if isinstance(payload, dict):
        raw = payload.get("signals", [])
        server_cursor = payload.get("cursor")
    elif isinstance(payload, list):
        raw = payload
        server_cursor = None
    else:
        log.warning("feed payload had unexpected shape; ignoring")
        return [], cursor

    events = [s for s in raw if _valid_signal(s)]
    dropped = len(raw) - len(events)
    if dropped:
        log.warning("dropped %s malformed signal(s) from feed", dropped)

    if server_cursor:
        new_cursor = str(server_cursor)
    else:
        new_cursor = _new_cursor(cursor, events)

    return events, new_cursor
