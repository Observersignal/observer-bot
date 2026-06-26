"""
state.py — local JSON persistence (state.json).

Keeps just enough state on the client's own machine to be safe and idempotent
across restarts:

  - processed_ids       : signal ids we have already acted on (never act twice)
  - cursor              : how far through the feed we have read
  - open_positions      : coin -> side, our local view of what we hold
  - day                 : current UTC date (YYYY-MM-DD)
  - realized_today_usd  : realized PnL accumulated today (resets each UTC day)

Nothing here ever leaves the machine.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("observer.state")

STATE_FILE = "state.json"

# Cap the processed-id history so state.json cannot grow without bound.
_MAX_PROCESSED = 5000


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class State:
    def __init__(self, data: dict):
        self.processed_ids = list(data.get("processed_ids", []))
        self.cursor = str(data.get("cursor", "") or "")
        self.open_positions = dict(data.get("open_positions", {}))
        self.day = str(data.get("day", "") or _today_utc())
        self.realized_today_usd = float(data.get("realized_today_usd", 0.0))
        # Fast membership lookups; kept in sync with processed_ids.
        self._processed_set = set(self.processed_ids)
        self._roll_day_if_needed()

    # -- day rollover ---------------------------------------------------- #
    def _roll_day_if_needed(self) -> None:
        today = _today_utc()
        if self.day != today:
            log.info(
                "UTC day rollover %s -> %s; resetting realized PnL counter",
                self.day,
                today,
            )
            self.day = today
            self.realized_today_usd = 0.0

    # -- idempotency ----------------------------------------------------- #
    def seen(self, signal_id: str) -> bool:
        return signal_id in self._processed_set

    def mark_processed(self, signal_id: str) -> None:
        if signal_id in self._processed_set:
            return
        self._processed_set.add(signal_id)
        self.processed_ids.append(signal_id)
        # Trim oldest if we exceed the cap.
        if len(self.processed_ids) > _MAX_PROCESSED:
            drop = len(self.processed_ids) - _MAX_PROCESSED
            removed = self.processed_ids[:drop]
            self.processed_ids = self.processed_ids[drop:]
            for r in removed:
                self._processed_set.discard(r)

    # -- positions ------------------------------------------------------- #
    def record_open(self, coin: str, side: str) -> None:
        self.open_positions[coin] = side

    def record_close(self, coin: str) -> None:
        self.open_positions.pop(coin, None)

    def count_open(self) -> int:
        return len(self.open_positions)

    # -- realized PnL ---------------------------------------------------- #
    def add_realized(self, usd: float) -> None:
        self._roll_day_if_needed()
        self.realized_today_usd += float(usd)

    # -- persistence ----------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "processed_ids": self.processed_ids,
            "cursor": self.cursor,
            "open_positions": self.open_positions,
            "day": self.day,
            "realized_today_usd": self.realized_today_usd,
        }

    def save(self, path: str = STATE_FILE) -> None:
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save state to %s: %s", path, exc)


def load(path: str = STATE_FILE) -> State:
    if not os.path.exists(path):
        return State({})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state file is not an object")
        return State(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        log.warning("could not read %s (%s); starting fresh", path, exc)
        return State({})
