"""
app.py — local web configuration + control UI for The Observer bot.

    python app.py

Starts a tiny web server bound to 127.0.0.1 ONLY, opens your browser to it,
and serves a single self-contained page where you can:

  1. Fill in your settings (capital, Hyperliquid keys, feed token, risk, safety)
     via forms — written ONLY to your local .env, never sent anywhere.
  2. See a big DRY-RUN / LIVE indicator and Start / Stop the bot.
  3. Watch live status: running state, open positions, realized-today, and the
     most recent log lines.

Everything stays on your machine. This is just a friendlier face over the same
`bot.py` loop — that still works standalone for a headless VPS.

NO third-party dependencies: pure Python stdlib (http.server). The trade SDK is
only needed once you go LIVE, exactly as for bot.py.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

import config
import state as state_mod
from bot import run_loop

# --- localhost only --------------------------------------------------------
# Bind to the loopback interface ONLY so the UI is never exposed on the LAN.
HOST = "127.0.0.1"
PORT = int(os.getenv("UI_PORT", "8765") or "8765")

# Host header values we accept. A request whose Host is anything else (e.g. a
# DNS-rebinding attacker's domain that resolves to 127.0.0.1) is rejected, so a
# malicious web page cannot drive this panel.
_ALLOWED_HOSTS = frozenset(
    f"{h}:{PORT}"
    for h in ("127.0.0.1", "localhost", "[::1]")
) | frozenset(("127.0.0.1", "localhost", "[::1]"))

# Origins we accept for cross-checking Origin/Referer on requests.
_ALLOWED_ORIGINS = frozenset(
    f"http://{h}:{PORT}" for h in ("127.0.0.1", "localhost", "[::1]")
)

# Per-process CSRF token. Injected into the served page and required as an
# X-CSRF-Token header on every mutating POST. A cross-origin page cannot read
# it (same-origin policy), so it cannot forge a valid request even via a
# no-preflight "simple" POST.
_CSRF_TOKEN = secrets.token_urlsafe(32)

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# Ring buffer of recent log lines, fed by the running loop's log sink.
_LOG_MAX = 200
_log_buffer: "deque[str]" = deque(maxlen=_LOG_MAX)
_log_lock = threading.Lock()

# Loop control. Only one loop thread may run at a time.
_loop_lock = threading.Lock()
_loop_thread: "Optional[threading.Thread]" = None
_stop_event: "Optional[threading.Event]" = None


def _log_sink(line: str) -> None:
    with _log_lock:
        _log_buffer.append(line)


def _recent_log(n: int = 30) -> "list[str]":
    with _log_lock:
        if n >= len(_log_buffer):
            return list(_log_buffer)
        return list(_log_buffer)[-n:]


def _is_running() -> bool:
    t = _loop_thread
    return t is not None and t.is_alive()


# ---------------------------------------------------------------------------
# Config <-> .env helpers (only touch KNOWN_ENV_KEYS, preserve everything else)
# ---------------------------------------------------------------------------
# Allowed value types for validation of incoming form fields.
_BOOL_KEYS = ("ISOLATED", "DRY_RUN", "ALLOW_FLIP", "ALLOW_INCREASE")
_FLOAT_KEYS = (
    "BASE_CAPITAL",
    "RISK_PER_TRADE_PCT",
    "LEVERAGE",
    "SIZE_USD",
    "DAILY_LOSS_LIMIT_USD",
)
_INT_KEYS = ("MAX_OPEN", "POLL_SECONDS")


def _parse_env_file(path: str) -> "list[Tuple[Optional[str], str]]":
    """
    Read .env into an ordered list of (key, raw_line).

    For "KEY=value" lines, key is the KEY; for comments/blanks, key is None and
    raw_line is preserved verbatim so we never clobber the user's own notes.
    """
    rows: "list[Tuple[Optional[str], str]]" = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh.read().splitlines():
                stripped = raw.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    rows.append((None, raw))
                    continue
                key = stripped.split("=", 1)[0].strip()
                rows.append((key, raw))
    except OSError:
        return rows
    return rows


def _write_env_file(path: str, updates: Dict[str, str]) -> None:
    """
    Write `updates` into .env, updating known keys in place and appending any
    that are missing. Comments and unknown keys are preserved verbatim. Written
    atomically via a temp file + os.replace.
    """
    rows = _parse_env_file(path)
    seen: set = set()
    out_lines: "list[str]" = []

    for key, raw in rows:
        if key is not None and key in updates:
            out_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out_lines.append(raw)

    # Append any known-but-missing keys at the end (e.g. brand-new .env).
    missing = [k for k in config.KNOWN_ENV_KEYS if k in updates and k not in seen]
    if missing:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        for k in missing:
            out_lines.append(f"{k}={updates[k]}")

    body = "\n".join(out_lines).rstrip("\n") + "\n"
    tmp = f"{path}.tmp"
    # Create the temp file with 0600 from the start so the secrets it holds
    # (HL_API_SECRET, OBSERVER_FEED_TOKEN) are never world-readable, not even
    # for the instant before os.replace. os.replace preserves the source mode.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, path)
    # If .env already existed with looser permissions, tighten it too.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _existing_env_values() -> Dict[str, str]:
    """Current raw values from .env (for keep-existing-secret behaviour)."""
    out: Dict[str, str] = {}
    for key, raw in _parse_env_file(ENV_PATH):
        if key is None:
            continue
        if "=" in raw:
            out[key] = raw.split("=", 1)[1].strip()
    return out


def _validate_and_normalise(body: Dict[str, Any]) -> Tuple[Dict[str, str], "list[str]"]:
    """
    Validate incoming form fields. Returns (updates, errors).

    `updates` maps env KEY -> string value, only for keys the user actually
    provided. Empty secret fields are dropped (means "keep existing"). On any
    validation error we return the collected messages and an empty update set.
    """
    errors: "list[str]" = []
    updates: Dict[str, str] = {}
    existing = _existing_env_values()

    def _has(k: str) -> bool:
        return k in body and body[k] is not None

    for key in config.KNOWN_ENV_KEYS:
        if not _has(key):
            continue
        val = body[key]

        # Reject control characters in any raw value: a newline would let a
        # single field inject extra KEY=value lines into .env (e.g. flipping
        # DRY_RUN off) on the next reload.
        if _has_control_chars(val):
            errors.append(f"{key} contains invalid characters")
            continue

        # Secrets: an empty field means "keep what's already there".
        if key in config.SECRET_ENV_KEYS:
            sval = str(val).strip()
            if sval == "":
                continue  # keep existing
            updates[key] = sval
            continue

        if key in _BOOL_KEYS:
            updates[key] = "true" if _coerce_bool(val) else "false"
            continue

        sval = str(val).strip()

        if key in _FLOAT_KEYS:
            if sval == "":
                continue
            try:
                num = float(sval)
            except ValueError:
                errors.append(f"{key} must be a number")
                continue
            if num < 0:
                errors.append(f"{key} cannot be negative")
                continue
            updates[key] = _fmt_num(num)
            continue

        if key in _INT_KEYS:
            if sval == "":
                continue
            try:
                num_i = int(float(sval))
            except ValueError:
                errors.append(f"{key} must be a whole number")
                continue
            if num_i < 0:
                errors.append(f"{key} cannot be negative")
                continue
            if key == "POLL_SECONDS" and num_i < 1:
                errors.append("POLL_SECONDS must be at least 1")
                continue
            updates[key] = str(num_i)
            continue

        # Plain strings (URLs, address).
        if key == "OBSERVER_API_URL" and sval and not sval.startswith(("http://", "https://")):
            errors.append("OBSERVER_API_URL must start with http:// or https://")
            continue
        updates[key] = sval

    # Don't bother writing identical no-op secret kept values; harmless anyway.
    _ = existing
    return (updates if not errors else {}), errors


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def _has_control_chars(val: Any) -> bool:
    """True if the string form contains CR/LF/NUL (would corrupt .env lines)."""
    return any(c in str(val) for c in ("\r", "\n", "\x00"))


def _fmt_num(num: float) -> str:
    """Format a float without a trailing .0 for whole numbers (clean .env)."""
    if num == int(num):
        return str(int(num))
    return repr(num)


# ---------------------------------------------------------------------------
# Status assembly (NEVER includes secrets)
# ---------------------------------------------------------------------------
def _build_status() -> Dict[str, Any]:
    cfg = config.CFG
    try:
        st = state_mod.load()
        open_positions = [
            {"coin": coin, "side": side} for coin, side in st.open_positions.items()
        ]
        realized_today = round(float(st.realized_today_usd), 2)
    except Exception:
        open_positions = []
        realized_today = 0.0

    # We don't probe the feed on every status poll — the explicit "Test
    # connection" action does that. Status just reports "unknown" here.
    feed_ok: Any = "unknown"

    config_present = os.path.exists(ENV_PATH)

    if cfg.configured_live():
        mode = "LIVE"
    else:
        mode = "DRY-RUN"

    return {
        "running": _is_running(),
        "dry_run": cfg.dry_run,
        "mode": mode,
        "base_capital": cfg.base_capital,
        "size_usd": cfg.size_usd,
        "risk_per_trade_pct": cfg.risk_per_trade_pct,
        "leverage": cfg.leverage,
        "isolated": cfg.isolated,
        "max_open": cfg.max_open,
        "daily_loss_limit_usd": cfg.daily_loss_limit_usd,
        "poll_seconds": cfg.poll_seconds,
        "allow_flip": cfg.allow_flip,
        "allow_increase": cfg.allow_increase,
        "feed_ok": feed_ok,
        # Secrets are masked to plain booleans-as-strings, never echoed.
        "feed_token": "set" if cfg.feed_token else "(not set)",
        # Days left on the subscription token (decoded from the token itself,
        # no secret needed). null if there's no token / can't be read.
        "token_days_left": (
            round(cfg.feed_token_days_left, 1)
            if cfg.feed_token_days_left is not None
            else None
        ),
        "api_secret": "set" if cfg.api_secret else "(not set)",
        "api_url": cfg.api_url,
        "account_address": cfg.account_address,
        "open_positions": open_positions,
        "realized_today": realized_today,
        "log": _recent_log(30),
        "config_present": config_present,
    }


def _config_summary_masked() -> Dict[str, Any]:
    """A safe (secret-masked) echo of the current config for POST responses."""
    cfg = config.CFG
    return {
        "base_capital": cfg.base_capital,
        "risk_per_trade_pct": cfg.risk_per_trade_pct,
        "size_usd": cfg.size_usd,
        "leverage": cfg.leverage,
        "isolated": cfg.isolated,
        "max_open": cfg.max_open,
        "daily_loss_limit_usd": cfg.daily_loss_limit_usd,
        "dry_run": cfg.dry_run,
        "poll_seconds": cfg.poll_seconds,
        "allow_flip": cfg.allow_flip,
        "allow_increase": cfg.allow_increase,
        "api_url": cfg.api_url,
        "account_address": cfg.account_address,
        "feed_token": "set" if cfg.feed_token else "(not set)",
        "api_secret": "set" if cfg.api_secret else "(not set)",
        "mode": "LIVE" if cfg.configured_live() else "DRY-RUN",
    }


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------
def _start_loop(confirm_live: bool = False) -> Tuple[bool, str]:
    global _loop_thread, _stop_event
    with _loop_lock:
        if _is_running():
            return False, "Bot is already running."

        config.reload()  # pick up the latest .env
        cfg = config.CFG

        # Going LIVE requires the keys. In DRY-RUN we allow starting freely so
        # the user can watch the mechanics (mock feed) before configuring.
        if not cfg.dry_run:
            missing = []
            if not cfg.feed_token:
                missing.append("feed token")
            if not cfg.account_address:
                missing.append("account address")
            if not cfg.api_secret:
                missing.append("API secret")
            if missing:
                return (
                    False,
                    "Cannot start LIVE without: "
                    + ", ".join(missing)
                    + ". Set them or keep DRY-RUN on.",
                )
            # Placing REAL orders is a dangerous action: require an explicit
            # confirmation flag so a single forged/accidental POST can't flip
            # the bot from practice into live trading.
            if cfg.configured_live() and not confirm_live:
                return (
                    False,
                    "LIVE start requires explicit confirmation (confirm_live).",
                )

        ev = threading.Event()
        t = threading.Thread(
            target=run_loop, args=(ev, _log_sink), name="observer-loop", daemon=True
        )
        _stop_event = ev
        _loop_thread = t
        t.start()
        return True, "LIVE — real orders will be placed." if not cfg.dry_run else "Started in DRY-RUN (no real orders)."


def _stop_loop() -> Tuple[bool, str]:
    global _loop_thread, _stop_event
    with _loop_lock:
        if not _is_running():
            return False, "Bot is not running."
        if _stop_event is not None:
            _stop_event.set()
        return True, "Stopping… the loop will save state and wind down."


# ---------------------------------------------------------------------------
# /api/test — best-effort connectivity checks (fast timeouts, never blocks)
# ---------------------------------------------------------------------------
def _test_feed() -> Dict[str, Any]:
    cfg = config.CFG
    if not cfg.feed_token:
        return {"ok": None, "message": "No feed token set (DEV mock mode)."}
    url = f"{cfg.api_url.rstrip('/')}/api/signals/feed?since=0"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {cfg.feed_token}",
            "X-Feed-Token": cfg.feed_token,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = resp.getcode()
        if code == 200:
            return {"ok": True, "message": "Feed token accepted (HTTP 200)."}
        return {"ok": False, "message": f"Feed returned HTTP {code}."}
    except urllib.error.HTTPError as exc:
        if exc.code == 401 or exc.code == 403:
            return {"ok": False, "message": f"Feed rejected the token (HTTP {exc.code})."}
        return {"ok": False, "message": f"Feed returned HTTP {exc.code}."}
    except urllib.error.URLError as exc:
        return {"ok": False, "message": f"Could not reach the feed: {exc.reason}."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Feed check failed: {exc}."}


def _test_hyperliquid() -> Dict[str, Any]:
    cfg = config.CFG
    if not cfg.account_address:
        return {"ok": None, "message": "No account address set."}
    # Best-effort: hit the public Info endpoint with clearinghouseState.
    url = "https://api.hyperliquid.xyz/info"
    payload = json.dumps(
        {"type": "clearinghouseState", "user": cfg.account_address}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = resp.getcode()
            raw = resp.read()
        if code != 200:
            return {"ok": False, "message": f"Hyperliquid Info returned HTTP {code}."}
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict) and "marginSummary" in data:
            try:
                acct_val = data["marginSummary"].get("accountValue")
            except Exception:  # noqa: BLE001
                acct_val = None
            if acct_val is not None:
                return {
                    "ok": True,
                    "message": f"Account reachable (value ≈ {acct_val} USDC).",
                }
            return {"ok": True, "message": "Account reachable."}
        return {"ok": True, "message": "Hyperliquid reachable (no positions yet)."}
    except urllib.error.URLError as exc:
        return {"ok": False, "message": f"Could not reach Hyperliquid: {exc.reason}."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Hyperliquid check failed: {exc}."}


def _run_tests() -> Dict[str, Any]:
    return {"feed": _test_feed(), "hyperliquid": _test_hyperliquid()}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ObserverUI/1.0"

    # Quieten the default request logging (it spams the console).
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    # -- helpers --------------------------------------------------------- #
    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None, "bad Content-Length"
        if length <= 0:
            return {}, None
        if length > 1_000_000:
            return None, "request too large"
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, "body is not valid JSON"
        if not isinstance(data, dict):
            return None, "body must be a JSON object"
        return data, None

    # -- request guard (anti-CSRF / anti-DNS-rebinding) ------------------ #
    def _guard(self, *, require_csrf: bool) -> Optional[str]:
        """
        Return an error string if the request must be rejected, else None.

        Defends the localhost-only panel against a malicious web page in the
        user's browser:
          - Host must be a local loopback host (defeats DNS-rebinding).
          - Origin/Referer, if present, must be a local origin (cross-site block).
          - Mutating POSTs must carry the per-process CSRF token, which a
            cross-origin page cannot read.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in _ALLOWED_HOSTS:
            return "bad host"

        origin = (self.headers.get("Origin") or "").strip()
        if origin and origin.lower() not in _ALLOWED_ORIGINS:
            return "cross-origin request refused"

        referer = (self.headers.get("Referer") or "").strip()
        if referer and not any(
            referer.lower().startswith(o) for o in _ALLOWED_ORIGINS
        ):
            return "cross-site referer refused"

        if require_csrf:
            token = self.headers.get("X-CSRF-Token") or ""
            if not secrets.compare_digest(token, _CSRF_TOKEN):
                return "missing or invalid CSRF token"
        return None

    # -- routes ---------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            blocked = self._guard(require_csrf=False)
            if blocked is not None:
                self._send_json({"error": "forbidden", "reason": blocked}, status=403)
                return
            if path == "/":
                self._send_html(_PAGE.replace("__CSRF_TOKEN__", _CSRF_TOKEN))
            elif path == "/api/status":
                self._send_json(_build_status())
            else:
                self._send_json({"error": "not found"}, status=404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._safe_500(exc)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            blocked = self._guard(require_csrf=True)
            if blocked is not None:
                self._send_json({"error": "forbidden", "reason": blocked}, status=403)
                return
            if path == "/api/config":
                self._handle_config()
            elif path == "/api/start":
                body, _ = self._read_json_body()
                confirm_live = bool((body or {}).get("confirm_live"))
                ok, msg = _start_loop(confirm_live=confirm_live)
                self._send_json(
                    {"ok": ok, "message": msg, "status": _build_status()},
                    status=200 if ok else 409,
                )
            elif path == "/api/stop":
                ok, msg = _stop_loop()
                self._send_json(
                    {"ok": ok, "message": msg, "status": _build_status()},
                    status=200 if ok else 409,
                )
            elif path == "/api/test":
                self._send_json({"ok": True, "results": _run_tests()})
            else:
                self._send_json({"error": "not found"}, status=404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._safe_500(exc)

    def _handle_config(self) -> None:
        body, err = self._read_json_body()
        if err is not None:
            self._send_json({"ok": False, "error": err}, status=400)
            return
        updates, errors = _validate_and_normalise(body or {})
        if errors:
            self._send_json({"ok": False, "errors": errors}, status=400)
            return
        if not updates:
            # Nothing to change — don't touch (or create) the .env file.
            self._send_json(
                {
                    "ok": True,
                    "message": "No changes to save.",
                    "config": _config_summary_masked(),
                }
            )
            return
        try:
            _write_env_file(ENV_PATH, updates)
        except OSError as exc:
            # Log the detail locally; don't leak the path/OSError to the client.
            _log_sink(f"could not write .env: {exc}")
            self._send_json(
                {"ok": False, "error": "could not save settings"}, status=500
            )
            return
        config.reload()
        self._send_json(
            {
                "ok": True,
                "message": "Saved. Changes apply on the next Start.",
                "config": _config_summary_masked(),
            }
        )

    def _safe_500(self, exc: Exception) -> None:
        # Log the detail locally for debugging; return a generic message so a
        # caller (incl. one that reached us cross-site) learns nothing internal.
        try:
            _log_sink(f"internal error: {exc}")
        except Exception:  # noqa: BLE001
            pass
        try:
            self._send_json({"error": "internal error"}, status=500)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# The page (self-contained, on-brand). No external assets.
# ---------------------------------------------------------------------------
_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="csrf-token" content="__CSRF_TOKEN__" />
<title>The Observer — Auto-Execution Bot</title>
<style>
  :root{
    --bg:#080808; --panel:#0f0f10; --panel2:#141416; --line:#222226;
    --ink:#e9e7e2; --mut:#8b8a86; --gold:#e7c876; --gold2:#caa14a;
    --green:#7fe0a6; --red:#f0a98a; --amber:#e7c876;
  }
  *{box-sizing:border-box}
  body{
    margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    -webkit-font-smoothing:antialiased;
  }
  a{color:var(--gold)}
  .wrap{max-width:980px;margin:0 auto;padding:24px 20px 64px}
  header{
    display:flex;align-items:center;gap:16px;flex-wrap:wrap;
    padding:18px 0 22px;border-bottom:1px solid var(--line);margin-bottom:24px;
  }
  .brand{font-size:18px;font-weight:600;letter-spacing:.3px}
  .brand .dot{color:var(--gold)}
  .brand small{display:block;color:var(--mut);font-size:12px;font-weight:400;letter-spacing:.2px}
  .spacer{flex:1}
  .pill{
    padding:8px 16px;border-radius:999px;font-weight:700;letter-spacing:.6px;
    font-size:13px;border:1px solid var(--line);
  }
  .pill.dry{background:rgba(231,200,118,.10);color:var(--amber);border-color:rgba(231,200,118,.35)}
  .pill.live{background:rgba(127,224,166,.10);color:var(--green);border-color:rgba(127,224,166,.35)}
  .pill.run{font-size:11px;padding:6px 12px}
  .pill.run.on{color:var(--green);border-color:rgba(127,224,166,.35)}
  .pill.run.off{color:var(--mut)}
  button{
    font:inherit;cursor:pointer;border-radius:10px;border:1px solid var(--line);
    background:var(--panel2);color:var(--ink);padding:10px 18px;transition:.12s;
  }
  button:hover{border-color:var(--gold2)}
  button:disabled{opacity:.4;cursor:not-allowed}
  .btn-start{background:linear-gradient(180deg,#1a2e22,#12201a);color:var(--green);border-color:rgba(127,224,166,.35);font-weight:700}
  .btn-stop{background:linear-gradient(180deg,#2e1a1a,#201212);color:var(--red);border-color:rgba(240,169,138,.35);font-weight:700}
  .btn-gold{background:linear-gradient(180deg,#241f12,#191509);color:var(--gold);border-color:rgba(231,200,118,.35);font-weight:700}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  @media(max-width:720px){.grid{grid-template-columns:1fr}}

  /* ---- View toggling: wizard vs control panel ---- */
  .view{display:none}
  .view.active{display:block}

  /* ---- Wizard shell ---- */
  .wizard{max-width:680px;margin:0 auto}
  .progress{margin-bottom:22px}
  .progress .ptop{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
  .progress .pstep{font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:var(--gold);font-weight:700}
  .progress .pskip{font-size:12px;color:var(--mut)}
  .progress .pskip a{cursor:pointer}
  .pbar{height:6px;background:var(--panel2);border:1px solid var(--line);border-radius:999px;overflow:hidden}
  .pbar > i{display:block;height:100%;width:20%;background:linear-gradient(90deg,var(--gold2),var(--gold));border-radius:999px;transition:width .25s ease}

  .step{display:none}
  .step.active{display:block}
  .step .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px 22px 24px}
  .step h1{margin:0 0 6px;font-size:18px;font-weight:600;letter-spacing:.2px;color:var(--ink)}
  .step .lede{color:var(--mut);font-size:13px;margin:0 0 18px;line-height:1.6}

  /* "What is this?" + "How to get it" blocks */
  .whatis{
    background:var(--panel2);border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;margin:0 0 16px;font-size:12.5px;line-height:1.6;color:var(--ink);
  }
  .whatis b{color:var(--gold);font-weight:600}
  .howto{
    background:linear-gradient(180deg,rgba(231,200,118,.06),rgba(231,200,118,.02));
    border:1px solid rgba(231,200,118,.30);border-radius:12px;
    padding:14px 16px 14px;margin:0 0 18px;
  }
  .howto .ht{font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--gold);font-weight:700;margin-bottom:8px}
  .howto ol{margin:0;padding-left:0;list-style:none;counter-reset:step}
  .howto li{
    position:relative;padding:5px 0 5px 34px;font-size:12.5px;line-height:1.5;
    counter-increment:step;color:var(--ink);
  }
  .howto li::before{
    content:counter(step);position:absolute;left:0;top:4px;
    width:22px;height:22px;border-radius:50%;
    background:rgba(231,200,118,.12);border:1px solid rgba(231,200,118,.4);color:var(--gold);
    font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;
  }
  .howto li b{color:var(--gold);font-weight:600}
  .howto li code{background:rgba(231,200,118,.10);border:1px solid rgba(231,200,118,.25);border-radius:5px;padding:1px 6px;color:var(--gold);font-size:12px}
  .howto .htnote{display:block;margin-top:10px;color:var(--mut);font-size:11.5px;line-height:1.55}

  .warn{
    display:flex;gap:9px;align-items:flex-start;
    background:rgba(240,169,138,.08);border:1px solid rgba(240,169,138,.35);
    border-radius:10px;padding:11px 13px;margin:14px 0 0;
    color:var(--red);font-size:12px;line-height:1.55;
  }
  .warn .si{flex:0 0 auto;font-size:14px;line-height:1.2}
  .warn b{color:var(--red);font-weight:700}

  /* ---- Reassurance note (DRY-RUN is safe) ---- */
  .safe-note{
    display:flex;gap:9px;align-items:flex-start;
    background:rgba(127,224,166,.07);border:1px solid rgba(127,224,166,.30);
    border-radius:10px;padding:10px 13px;margin:14px 0 0;
    color:var(--green);font-size:12px;line-height:1.5;
  }
  .safe-note.big{padding:14px 16px;font-size:13px}
  .safe-note .si{flex:0 0 auto;font-size:14px;line-height:1.2}
  .safe-note b{color:var(--green);font-weight:700}

  /* ---- Collapsible "advanced" section ---- */
  details.adv{margin:14px 0 0;border:1px solid var(--line);border-radius:10px;background:var(--panel2);overflow:hidden}
  details.adv > summary{
    cursor:pointer;list-style:none;padding:11px 14px;font-size:12px;color:var(--gold);
    font-weight:600;letter-spacing:.3px;user-select:none;
  }
  details.adv > summary::-webkit-details-marker{display:none}
  details.adv > summary::before{content:"▸ ";color:var(--gold2)}
  details.adv[open] > summary::before{content:"▾ "}
  details.adv .advbody{padding:4px 14px 14px;border-top:1px solid var(--line)}

  /* ---- Wizard nav ---- */
  .wnav{display:flex;gap:10px;margin-top:22px;align-items:center;flex-wrap:wrap}
  .wnav .spacer{flex:1}
  .wnav .btn-back{background:var(--panel2)}
  .stepmsg{
    margin-top:14px;font-size:12.5px;padding:9px 13px;border-radius:8px;display:none;line-height:1.5;
    background:rgba(240,169,138,.08);border:1px solid rgba(240,169,138,.30);color:var(--red);
  }
  .stepmsg.show{display:block}

  /* ---- Shared card / fields ---- */
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 18px 20px}
  .card h2{margin:0 0 4px;font-size:13px;letter-spacing:.6px;text-transform:uppercase;color:var(--gold)}
  .card .sub{color:var(--mut);font-size:12px;margin:0 0 14px}
  .field{margin-bottom:16px}
  .field>label{display:block;font-size:12px;color:var(--ink);margin-bottom:5px;letter-spacing:.2px;font-weight:600}
  .field input[type=text],.field input[type=password],.field input[type=number]{
    width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:9px;
    color:var(--ink);padding:9px 11px;font:inherit;
  }
  .field input:focus{outline:none;border-color:var(--gold2)}
  .hint{color:var(--gold);font-size:12px;margin-top:6px}
  .help{color:var(--mut);font-size:11.5px;line-height:1.55;margin-top:6px}
  .help b{color:var(--ink);font-weight:600}
  .two{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:720px){.two{grid-template-columns:1fr}}

  /* ---- iOS-style toggle (self-contained; never inherits .field label) ---- */
  .toggle{
    display:flex;align-items:flex-start;gap:12px;cursor:pointer;user-select:none;
    margin:0;font-weight:400;
  }
  /* Hide the checkbox visually but keep it focusable/clickable. */
  .toggle input{
    position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
    clip:rect(0 0 0 0);white-space:nowrap;border:0;
  }
  .toggle .sw{
    flex:0 0 auto;display:inline-block;position:relative;
    width:44px;height:26px;border-radius:999px;
    background:var(--panel2);border:1px solid var(--line);
    transition:background .18s ease,border-color .18s ease;margin-top:1px;
  }
  .toggle .sw::after{
    content:"";position:absolute;top:2px;left:2px;
    width:20px;height:20px;border-radius:50%;
    background:var(--mut);transition:transform .18s ease,background .18s ease;
  }
  .toggle input:checked + .sw{background:rgba(231,200,118,.30);border-color:var(--gold2)}
  .toggle input:checked + .sw::after{transform:translateX(18px);background:var(--gold)}
  /* Green variant for the safety-positive DRY-RUN switch. */
  .toggle.green input:checked + .sw{background:rgba(127,224,166,.25);border-color:rgba(127,224,166,.5)}
  .toggle.green input:checked + .sw::after{background:var(--green)}
  .toggle input:focus-visible + .sw{box-shadow:0 0 0 3px rgba(231,200,118,.35)}
  .toggle .tl{font-size:13px;color:var(--ink);font-weight:600;line-height:1.35}
  .toggle .td{display:block;color:var(--mut);font-size:11.5px;font-weight:400;line-height:1.5;margin-top:3px}
  .row-actions{display:flex;gap:10px;margin-top:8px;flex-wrap:wrap}
  .full{grid-column:1/-1}
  .status-line{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed var(--line)}
  .status-line:last-child{border-bottom:none}
  .status-line .k{color:var(--mut)}
  .status-line .v{font-weight:600}
  .v.green{color:var(--green)} .v.red{color:var(--red)} .v.gold{color:var(--gold)}
  .poslist{margin:8px 0 0;padding:0;list-style:none}
  .poslist li{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line)}
  .poslist .long{color:var(--green)} .poslist .short{color:var(--red)}
  .empty{color:var(--mut);font-style:italic;font-size:12px}
  .log{
    background:#050505;border:1px solid var(--line);border-radius:10px;padding:10px 12px;
    height:220px;overflow:auto;font-size:11.5px;line-height:1.55;white-space:pre-wrap;color:#cfd2c9;
  }
  .testbox{margin-top:12px;font-size:12.5px}
  .testbox .tr{display:flex;gap:9px;padding:6px 10px;border-radius:8px;line-height:1.5;margin-bottom:6px}
  .testbox .tr.ok{background:rgba(127,224,166,.07);border:1px solid rgba(127,224,166,.25)}
  .testbox .tr.fail{background:rgba(240,169,138,.07);border:1px solid rgba(240,169,138,.25)}
  .testbox .tr.na{background:var(--panel2);border:1px solid var(--line)}
  .testbox .ico{width:16px;flex:0 0 auto;font-weight:700}
  .testbox .fix{display:block;color:var(--mut);font-size:11.5px;margin-top:3px}
  .ok{color:var(--green)} .fail{color:var(--red)} .na{color:var(--mut)}
  /* Inline save result line */
  .result{margin-top:12px;font-size:12.5px;padding:9px 13px;border-radius:8px;display:none;line-height:1.5}
  .result.show{display:block}
  .result.good{background:rgba(127,224,166,.07);border:1px solid rgba(127,224,166,.25);color:var(--green)}
  .result.bad{background:rgba(240,169,138,.07);border:1px solid rgba(240,169,138,.25);color:var(--red)}
  .result .fix{display:block;color:var(--mut);font-size:11.5px;margin-top:3px}
  .toast{
    position:fixed;left:50%;bottom:24px;transform:translateX(-50%);
    background:var(--panel2);border:1px solid var(--gold2);color:var(--ink);
    padding:11px 18px;border-radius:10px;opacity:0;pointer-events:none;transition:.2s;font-size:13px;max-width:90%;
  }
  .toast.show{opacity:1}
  .toast.err{border-color:rgba(240,169,138,.5)}
  .foot{color:var(--mut);font-size:11px;margin-top:26px;text-align:center;line-height:1.7}
  .relink{font-size:12px;color:var(--mut);margin-top:18px;text-align:center}
  .relink a{cursor:pointer}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">The Observer<span class="dot">.</span><small>Auto-Execution Bot · runs on your machine</small></div>
    <div class="spacer"></div>
    <span id="runPill" class="pill run off">● STOPPED</span>
    <span id="modePill" class="pill dry">DRY-RUN</span>
    <button id="toggleBtn" class="btn-start" onclick="toggleRun()">Start</button>
  </header>

  <!-- ============================================================= -->
  <!-- VIEW 1 · SETUP WIZARD                                          -->
  <!-- ============================================================= -->
  <div id="wizardView" class="view">
    <div class="wizard">
      <div class="progress">
        <div class="ptop">
          <span class="pstep" id="progLabel">Step 1 of 5</span>
          <span class="pskip"><a onclick="showPanel()">Skip to control panel →</a></span>
        </div>
        <div class="pbar"><i id="progBar"></i></div>
      </div>

      <!-- STEP 1 · Welcome -->
      <section class="step" data-step="1">
        <div class="card">
          <h1>Welcome — let's set up your bot</h1>
          <p class="lede">It runs on <b>YOUR</b> machine, with <b>YOUR</b> keys — we never touch your funds.
            The whole thing takes about 5 minutes.</p>
          <div class="safe-note big"><span class="si">●</span><span>You can stay in <b>DRY-RUN (practice mode)</b> the whole way through — the bot only
            simulates and places <b>no real orders</b> until you decide to go live.</span></div>
          <div class="wnav">
            <div class="spacer"></div>
            <button class="btn-gold" onclick="next()">Start setup →</button>
          </div>
        </div>
      </section>

      <!-- STEP 2 · Feed token -->
      <section class="step" data-step="2">
        <div class="card">
          <h1>Your signal access</h1>
          <p class="lede">A personal key that lets your bot receive The Observer's trading signals.</p>
          <div class="whatis"><b>What is this?</b> Your personal key to receive the trading signals. The bot uses it to
            pull each signal as it fires.</div>
          <div class="howto">
            <div class="ht">How to get it</div>
            <ol>
              <li>Open <b>Telegram</b>.</li>
              <li>Message <b>The Observer</b> access bot.</li>
              <li>Send <code>/bot</code>.</li>
              <li>It replies with your token — <b>copy it</b> and paste it below.</li>
            </ol>
            <span class="htnote">No token yet? Leave it blank — you can try the bot in a safe local demo first and
              add your token later.</span>
          </div>
          <div class="field">
            <label>Feed token</label>
            <input type="password" id="OBSERVER_FEED_TOKEN" placeholder="•••• (leave blank to keep)" autocomplete="new-password" spellcheck="false" />
            <div class="help">Paste the token Telegram gave you. Leave blank to run the local demo (DEV mock).</div>
          </div>
          <details class="adv">
            <summary>Advanced (optional)</summary>
            <div class="advbody">
              <div class="field">
                <label>API URL</label>
                <input type="text" id="OBSERVER_API_URL" spellcheck="false" />
                <div class="help">Where the bot fetches signals from. The default is already correct — leave it as is
                  unless you're told otherwise.</div>
              </div>
            </div>
          </details>
          <div class="stepmsg" id="msg2"></div>
          <div class="wnav">
            <button class="btn-back" onclick="back()">← Back</button>
            <div class="spacer"></div>
            <button class="btn-gold" onclick="next()">Next →</button>
          </div>
        </div>
      </section>

      <!-- STEP 3 · Hyperliquid account -->
      <section class="step" data-step="3">
        <div class="card">
          <h1>Your Hyperliquid account</h1>
          <p class="lede">The bot trades on <b>your</b> account using a trade-only key.</p>
          <div class="whatis"><b>What is this?</b> The bot trades on YOUR Hyperliquid account using a
            <b>trade-only key</b> — a key that can place trades but <b>cannot withdraw</b> your money.</div>
          <div class="howto">
            <div class="ht">How to get it</div>
            <ol>
              <li>In Hyperliquid, open the <b>API</b> / agent-wallet section (in your account menu).</li>
              <li>Create / generate a new <b>API wallet</b> (also called an agent wallet) and authorize it.</li>
              <li>Copy its <b>private key</b> and paste it in <b>API wallet key</b> below.</li>
              <li>Copy your <b>main wallet address</b> (the <code>0x…</code> that holds your funds) into <b>Account address</b>.</li>
            </ol>
          </div>
          <div class="warn"><span class="si">⚠</span><span><b>Never</b> paste your main wallet's seed phrase or private key — only the
            API / agent wallet key. If it ever leaks, nobody can withdraw your funds with it.</span></div>
          <div class="field">
            <label>Account address (0x…)</label>
            <input type="text" id="HL_ACCOUNT_ADDRESS" placeholder="0x…" autocomplete="off" spellcheck="false" />
            <div class="help">Your Hyperliquid wallet address — the public <b>0x…</b> that holds your funds.</div>
          </div>
          <div class="field">
            <label>API wallet key</label>
            <input type="password" id="HL_API_SECRET" placeholder="•••• (leave blank to keep)" autocomplete="new-password" spellcheck="false" />
            <div class="help"><b>NOT your main wallet key.</b> This is the private key of the API / agent wallet you
              created above. It can place trades but <b>cannot withdraw your funds</b>.</div>
          </div>
          <div class="stepmsg" id="msg3"></div>
          <div class="wnav">
            <button class="btn-back" onclick="back()">← Back</button>
            <div class="spacer"></div>
            <button class="btn-gold" onclick="next()">Next →</button>
          </div>
        </div>
      </section>

      <!-- STEP 4 · Size & limits -->
      <section class="step" data-step="4">
        <div class="card">
          <h1>Your size &amp; limits</h1>
          <p class="lede">Just enter your capital — the bot sizes every trade for you.</p>
          <div class="whatis"><b>What is this?</b> Enter your total trading capital — the bot automatically sizes
            each trade to the same proportion as the model (<b>1%</b>). You don't size trades by hand.</div>
          <div class="field">
            <label>Base capital (USD)</label>
            <input type="number" id="BASE_CAPITAL" step="any" oninput="recalcHint()" />
            <div class="hint" id="sizeHint">≈ — per trade</div>
            <div class="help">Your total trading capital. The bot risks the same proportion per trade as the model
              (1%). Enter <b>5000</b> and it uses about <b>$50</b> per trade; enter <b>20000</b> and it uses about
              <b>$200</b>. You never size each trade by hand.</div>
          </div>
          <details class="adv">
            <summary>Advanced (optional)</summary>
            <div class="advbody">
              <div class="field">
                <label>Risk per trade (%)</label>
                <input type="number" id="RISK_PER_TRADE_PCT" step="any" oninput="recalcHint()" />
                <div class="help">How much of your capital each trade uses as margin. <b>1%</b> mirrors the model.
                  Lower it to trade smaller.</div>
              </div>
              <div class="field">
                <label>Leverage (x)</label>
                <input type="number" id="LEVERAGE" step="any" />
                <div class="help">Leverage applied to each trade. The model uses <b>10x</b>.</div>
              </div>
              <div class="field">
                <label class="toggle"><input type="checkbox" id="ISOLATED" /><span class="sw"></span>
                  <span class="tl">Isolated margin<span class="td">Each position keeps its own margin (what the model
                    uses), so one bad trade can't drain the others. Off = cross margin (shared).</span></span></label>
              </div>
              <div class="field">
                <label>Max open positions</label>
                <input type="number" id="MAX_OPEN" step="1" />
                <div class="help">The most positions the bot will hold at the same time.</div>
              </div>
              <div class="field">
                <label>Daily loss limit (USD)</label>
                <input type="number" id="DAILY_LOSS_LIMIT_USD" step="any" />
                <div class="help">If your realized loss today reaches this amount, the bot stops opening <b>new</b>
                  trades for the rest of the day. It still closes existing ones.</div>
              </div>
            </div>
          </details>
          <div class="stepmsg" id="msg4"></div>
          <div class="wnav">
            <button class="btn-back" onclick="back()">← Back</button>
            <div class="spacer"></div>
            <button class="btn-gold" onclick="next()">Next →</button>
          </div>
        </div>
      </section>

      <!-- STEP 5 · Test & start -->
      <section class="step" data-step="5">
        <div class="card">
          <h1>Test &amp; start</h1>
          <p class="lede">Save your setup, check the connections, and you're ready.</p>
          <div class="field">
            <label class="toggle green"><input type="checkbox" id="DRY_RUN" /><span class="sw"></span>
              <span class="tl">DRY-RUN (practice mode)<span class="td">ON = the bot only simulates and places NO real
                orders. Start here. Turn OFF only when you're ready for real trading.</span></span></label>
          </div>
          <div class="safe-note big"><span class="si">●</span><span>Leave this <b>ON</b> to practice safely — nothing happens to your money.
            You can come back and turn it off whenever you're ready.</span></div>
          <details class="adv">
            <summary>Advanced (optional)</summary>
            <div class="advbody">
              <div class="field">
                <label>Poll interval (seconds)</label>
                <input type="number" id="POLL_SECONDS" step="1" />
                <div class="help">How often the bot checks for new signals. <b>4</b> is fine.</div>
              </div>
              <div class="field">
                <label class="toggle"><input type="checkbox" id="ALLOW_FLIP" /><span class="sw"></span>
                  <span class="tl">Allow flips<span class="td">If a signal flips direction, close the current position
                    and open the opposite one.</span></span></label>
              </div>
              <div class="field">
                <label class="toggle"><input type="checkbox" id="ALLOW_INCREASE" /><span class="sw"></span>
                  <span class="tl">Allow increases<span class="td">Add to a position you already hold if the signal
                    says so. Off by default (more conservative).</span></span></label>
              </div>
            </div>
          </details>
          <div class="row-actions">
            <button class="btn-gold" onclick="saveAndTest()">Save &amp; test</button>
            <button onclick="finishToPanel()">Finish — go to the panel →</button>
          </div>
          <div class="result" id="saveResult"></div>
          <div class="testbox" id="testbox"></div>
          <div class="wnav">
            <button class="btn-back" onclick="back()">← Back</button>
            <div class="spacer"></div>
          </div>
        </div>
      </section>
    </div>
  </div>

  <!-- ============================================================= -->
  <!-- VIEW 2 · CONTROL PANEL                                         -->
  <!-- ============================================================= -->
  <div id="panelView" class="view">
    <div class="card full" style="margin-bottom:18px">
      <h2>Live status</h2>
      <p class="sub">Polls every 3s · everything stays on your machine.</p>
      <div class="grid">
        <div>
          <div class="status-line"><span class="k">State</span><span class="v" id="stState">—</span></div>
          <div class="status-line"><span class="k">Mode</span><span class="v" id="stMode">—</span></div>
          <div class="status-line"><span class="k">Margin / trade</span><span class="v" id="stSize">—</span></div>
          <div class="status-line"><span class="k">Realized today</span><span class="v" id="stRealized">—</span></div>
          <div class="status-line"><span class="k">Feed token</span><span class="v" id="stFeed">—</span></div>
          <div class="status-line"><span class="k">Access left</span><span class="v" id="stAccess">—</span></div>
          <div class="status-line"><span class="k">API secret</span><span class="v" id="stSecret">—</span></div>
          <h2 style="margin-top:16px">Open positions</h2>
          <ul class="poslist" id="posList"><li class="empty">none</li></ul>
        </div>
        <div>
          <h2>Log</h2>
          <div class="log" id="log"><span class="empty">No log lines yet. Press Start.</span></div>
        </div>
      </div>
      <div class="row-actions" style="margin-top:16px">
        <button onclick="runTest('testboxPanel')">Test connection</button>
      </div>
      <div class="testbox" id="testboxPanel"></div>
    </div>
    <div class="relink">Need to change something? <a onclick="showWizard()">Edit settings / re-run setup →</a></div>
  </div>

  <p class="foot">
    Non-custodial · your keys live only in your local <code>.env</code>, never sent anywhere.<br/>
    Bound to 127.0.0.1 — not reachable from your network. Close this tab and the bot keeps running until you Stop it or close the program.
  </p>
</div>
<div class="toast" id="toast"></div>

<script>
const FIELDS = ["BASE_CAPITAL","RISK_PER_TRADE_PCT","LEVERAGE","ISOLATED",
  "HL_ACCOUNT_ADDRESS","HL_API_SECRET","OBSERVER_FEED_TOKEN","OBSERVER_API_URL",
  "MAX_OPEN","DAILY_LOSS_LIMIT_USD","DRY_RUN","POLL_SECONDS","ALLOW_FLIP","ALLOW_INCREASE"];
const BOOLS = ["ISOLATED","DRY_RUN","ALLOW_FLIP","ALLOW_INCREASE"];
const SECRETS = ["HL_API_SECRET","OBSERVER_FEED_TOKEN"];
const TOTAL_STEPS = 5;
let running = false;
let curStep = 1;
let prefilled = false;         // /api/status loaded into the form at least once
let feedTokenSet = false;      // does .env already have a feed token?
let addressSet = false;        // does .env already have an account address?

function $(id){return document.getElementById(id);}

// CSRF token injected by the server into a meta tag; sent on every mutating POST.
const CSRF = (document.querySelector('meta[name="csrf-token"]')||{}).content || "";
function postJSON(url, body){
  return fetch(url, {
    method:"POST",
    headers:{"Content-Type":"application/json","X-CSRF-Token":CSRF},
    body: body!==undefined ? JSON.stringify(body) : "{}",
  });
}

function toast(msg, isErr){
  const t=$("toast"); t.textContent=msg;
  t.className="toast show"+(isErr?" err":"");
  clearTimeout(t._h); t._h=setTimeout(()=>{t.className="toast";},3200);
}

function recalcHint(){
  const cap=parseFloat($("BASE_CAPITAL").value)||0;
  const pct=parseFloat($("RISK_PER_TRADE_PCT").value)||0;
  const m=(cap*pct/100);
  $("sizeHint").textContent = m>0
    ? "≈ $"+m.toLocaleString(undefined,{maximumFractionDigits:2})+" per trade ("+pct+"% of your capital)"
    : "≈ — per trade";
}

// ---------------------------------------------------------------------------
// View switching (wizard <-> control panel). No routes change; purely visual.
// ---------------------------------------------------------------------------
function showWizard(){
  curStep = 1;
  renderStep();
  $("panelView").classList.remove("active");
  $("wizardView").classList.add("active");
  window.scrollTo(0,0);
}
function showPanel(){
  $("wizardView").classList.remove("active");
  $("panelView").classList.add("active");
  window.scrollTo(0,0);
}
function finishToPanel(){ showPanel(); }

// Pick the initial view from /api/status: if neither a feed token nor an
// account address is set, the bot isn't configured yet -> show the wizard.
function chooseInitialView(){
  if(feedTokenSet || addressSet){ showPanel(); }
  else { showWizard(); }
}

// ---------------------------------------------------------------------------
// Wizard step navigation
// ---------------------------------------------------------------------------
function renderStep(){
  document.querySelectorAll(".step").forEach(s=>{
    s.classList.toggle("active", Number(s.getAttribute("data-step"))===curStep);
  });
  $("progLabel").textContent = "Step "+curStep+" of "+TOTAL_STEPS;
  $("progBar").style.width = Math.round(curStep/TOTAL_STEPS*100)+"%";
  // Clear any stale step messages when moving.
  for(let i=2;i<=5;i++){ const m=$("msg"+i); if(m) m.className="stepmsg"; }
}

function stepError(step, text){
  const m=$("msg"+step);
  if(m){ m.textContent=text; m.className="stepmsg show"; }
}

// Validate the current step's fields. Step 1 is skippable (try the demo first).
function validateStep(step){
  if(step===2){
    // Feed token may be blank (demo). If left blank AND nothing already set,
    // we simply allow it — the user opted into the local demo.
    return true;
  }
  if(step===3){
    const addr = $("HL_ACCOUNT_ADDRESS").value.trim();
    const sec  = $("HL_API_SECRET").value.trim();
    // Allow fully blank (keep DRY-RUN demo). But if they typed an address, it
    // must look like a 0x… hex string; warn on an obvious mistake.
    if(addr && !/^0x[0-9a-fA-F]{6,}$/.test(addr)){
      stepError(3, "That account address doesn't look right — it should start with 0x followed by hex characters.");
      return false;
    }
    // If they entered an address but no key (and none is stored), nudge them —
    // but still allow proceeding so they can practice in DRY-RUN.
    if(addr && !sec && !addressSet){
      // Soft nudge only; not a hard block.
    }
    return true;
  }
  if(step===4){
    const cap = $("BASE_CAPITAL").value.trim();
    if(cap===""){
      stepError(4, "Please enter your base capital (e.g. 5000) so the bot can size each trade.");
      return false;
    }
    const n = parseFloat(cap);
    if(isNaN(n) || n<=0){
      stepError(4, "Base capital must be a positive number, like 5000.");
      return false;
    }
    return true;
  }
  return true;
}

function next(){
  if(!validateStep(curStep)) return;
  if(curStep < TOTAL_STEPS){
    curStep++;
    renderStep();
    window.scrollTo(0,0);
  }
}
function back(){
  if(curStep > 1){
    curStep--;
    renderStep();
    window.scrollTo(0,0);
  }
}

// ---------------------------------------------------------------------------
// Prefill forms from /api/status (secrets shown as placeholder only).
// ---------------------------------------------------------------------------
async function prefill(){
  try{
    const r=await fetch("/api/status"); const s=await r.json();
    feedTokenSet = s.feed_token==="set";
    addressSet   = !!(s.account_address && String(s.account_address).trim());
    $("BASE_CAPITAL").value=s.base_capital;
    $("RISK_PER_TRADE_PCT").value=s.risk_per_trade_pct;
    $("LEVERAGE").value=s.leverage;
    $("ISOLATED").checked=!!s.isolated;
    $("HL_ACCOUNT_ADDRESS").value=s.account_address||"";
    $("OBSERVER_API_URL").value=s.api_url||"";
    $("MAX_OPEN").value=s.max_open;
    $("DAILY_LOSS_LIMIT_USD").value=s.daily_loss_limit_usd;
    $("DRY_RUN").checked=!!s.dry_run;
    $("POLL_SECONDS").value=s.poll_seconds;
    $("ALLOW_FLIP").checked=!!s.allow_flip;
    $("ALLOW_INCREASE").checked=!!s.allow_increase;
    // Secrets: never receive the value. Show a placeholder if already set.
    $("HL_API_SECRET").placeholder = s.api_secret==="set" ? "•••• (set — leave blank to keep)" : "(not set)";
    $("OBSERVER_FEED_TOKEN").placeholder = s.feed_token==="set" ? "•••• (set — leave blank to keep)" : "(not set — DEV mock)";
    recalcHint();
    // On the very first load, choose which view to show.
    if(!prefilled){ prefilled=true; chooseInitialView(); }
  }catch(e){
    // Page still usable; default to the wizard if we couldn't read status.
    if(!prefilled){ prefilled=true; showWizard(); }
  }
}

function collect(){
  const body={};
  for(const id of FIELDS){
    const el=$(id);
    if(BOOLS.includes(id)) body[id]=el.checked;
    else body[id]=el.value;
  }
  // Don't send empty secrets (means keep existing).
  for(const k of SECRETS){ if(!body[k]) delete body[k]; }
  return body;
}

// Show an inline result line (good=green, bad=red). `fix` is an optional
// one-line "what to do" shown under the main message.
function showResult(good, msg, fix){
  const el=$("saveResult");
  el.className="result show "+(good?"good":"bad");
  el.innerHTML = (good?"✓ ":"⚠ ")+msg + (fix?'<span class="fix">'+fix+'</span>':"");
  clearTimeout(el._h);
  if(good) el._h=setTimeout(()=>{el.className="result";}, 6000);
}

// POST the whole collected config. Returns true on success.
async function saveConfig(){
  try{
    const r=await postJSON("/api/config", collect());
    const d=await r.json();
    if(d.ok){
      $("HL_API_SECRET").value=""; $("OBSERVER_FEED_TOKEN").value=""; prefill();
      showResult(true, "Settings saved. They'll apply the next time you press Start.");
      return true;
    } else {
      console.error("Save failed:", d.errors||d.error);
      showResult(false, friendlySaveError(d), "Check the highlighted value and try Save again.");
      return false;
    }
  }catch(e){
    console.error("Save request failed:", e);
    showResult(false, "Couldn't save your settings.", "Make sure the control panel is still running and try again.");
    return false;
  }
}

// Step 5 primary action: save the full config, then run the connectivity test.
async function saveAndTest(){
  const ok = await saveConfig();
  if(ok){ await runTest("testbox"); }
}

// Map server validation errors to a single human sentence (no field codes).
function friendlySaveError(d){
  const errs = d.errors||[];
  const j = errs.join(" ");
  if(/POLL_SECONDS/.test(j)) return "The poll interval needs to be a whole number of at least 1 second.";
  if(/negative/.test(j))     return "One of the numbers can't be negative.";
  if(/number|whole/.test(j)) return "One of the values isn't a valid number.";
  if(/OBSERVER_API_URL/.test(j)) return "The API URL must start with http:// or https://.";
  if(errs.length) return "Some values couldn't be saved — please check the numbers you entered.";
  return "Couldn't save your settings.";
}

// Run the connectivity test. `target` is the id of the testbox to render into
// (the wizard's step-5 box or the control-panel box).
async function runTest(target){
  const boxId = (typeof target==="string") ? target : "testbox";
  const box=$(boxId);
  box.innerHTML='<div class="tr na"><span class="ico">•</span><span>Checking your connection…</span></div>';
  try{
    const r=await postJSON("/api/test"); const d=await r.json();
    const res=d.results||{};
    box.innerHTML = row("Signal feed", res.feed) + row("Hyperliquid account", res.hyperliquid);
  }catch(e){
    console.error("Test request failed:", e);
    box.innerHTML='<div class="tr fail"><span class="ico">✕</span><span><b>Connection test:</b> couldn\'t run the check.<span class="fix">Make sure the control panel is still running and try again.</span></span></div>';
  }
}

// Plain-language results for the connectivity test. Technical detail (HTTP
// codes etc.) is logged to the console, never shown to the user.
function row(label, r){
  if(!r) return "";
  let cls="na", ico="•", msg=r.message||"", fix="";
  if(r.ok===true){
    cls="ok"; ico="✓";
    msg = label==="Signal feed" ? "Connected — your token works." : "Connected — your account is reachable.";
  } else if(r.ok===false){
    cls="fail"; ico="✕";
    console.warn(label+" test:", r.message);
    if(label==="Signal feed"){
      if(/reject|401|403|token/i.test(r.message)){ msg="That feed token wasn't accepted."; fix="Re-copy it from Telegram (send /bot to the access bot) and Save again."; }
      else { msg="Couldn't reach the signal feed."; fix="Check your internet connection, then try again."; }
    } else {
      if(/reach|URL|connection/i.test(r.message)){ msg="Couldn't reach Hyperliquid."; fix="Check your internet connection, then try again."; }
      else { msg="Couldn't read that account."; fix="Double-check your account address (the 0x… one) and Save again."; }
    }
  } else {
    // ok === null: not configured yet.
    msg = label==="Signal feed" ? "No token set — running the local demo." : "No account address set yet.";
  }
  return '<div class="tr '+cls+'"><span class="ico">'+ico+'</span><span><b>'+label+':</b> '+msg+(fix?'<span class="fix">'+fix+'</span>':"")+'</span></div>';
}

async function toggleRun(){
  const btn=$("toggleBtn"); btn.disabled=true;
  try{
    const ep = running ? "/api/stop" : "/api/start";
    let body;
    if(!running && $("modePill").textContent.trim()==="LIVE"){
      // Starting in LIVE places REAL orders — require an explicit confirmation.
      if(!confirm("Start in LIVE mode? The bot will place REAL orders on your Hyperliquid account.")){
        btn.disabled=false; return;
      }
      body = {confirm_live:true};
    }
    const r=await postJSON(ep, body); const d=await r.json();
    if(!d.ok) console.warn("Start/stop:", d.message);
    toast(friendlyRunMessage(d), !d.ok);
    if(d.status) render(d.status);
  }catch(e){
    console.error("Start/stop request failed:", e);
    toast("Couldn't reach the bot — is the control panel still running?", true);
  }
  finally{ btn.disabled=false; }
}

// Keep the server's clear messages, but soften the LIVE-missing-keys case.
function friendlyRunMessage(d){
  const m = d.message||"";
  if(!d.ok && /without:/i.test(m)){
    return "Can't go live yet — add your feed token, account address and API key first (or keep DRY-RUN on to practice).";
  }
  return m || (d.ok?"Done.":"That didn't work.");
}

function render(s){
  running = !!s.running;
  const rp=$("runPill");
  rp.textContent = running ? "● RUNNING" : "● STOPPED";
  rp.className = "pill run "+(running?"on":"off");
  const mp=$("modePill");
  mp.textContent = s.mode;
  mp.className = "pill "+(s.mode==="LIVE"?"live":"dry");
  const tb=$("toggleBtn");
  tb.textContent = running ? "Stop" : "Start";
  tb.className = running ? "btn-stop" : "btn-start";

  $("stState").textContent = running ? "running" : "stopped";
  $("stState").className = "v "+(running?"green":"");
  $("stMode").textContent = s.mode;
  $("stMode").className = "v "+(s.mode==="LIVE"?"green":"gold");
  $("stSize").textContent = "$"+Number(s.size_usd).toLocaleString(undefined,{maximumFractionDigits:2})+" @ "+s.leverage+"x";
  const rt=Number(s.realized_today)||0;
  $("stRealized").textContent = (rt>=0?"+":"")+"$"+rt.toFixed(2);
  $("stRealized").className = "v "+(rt>0?"green":(rt<0?"red":""));
  $("stFeed").textContent = s.feed_token;
  $("stFeed").className = "v "+(s.feed_token==="set"?"green":"");
  const dl = s.token_days_left;
  const acc = $("stAccess");
  if(dl===null || dl===undefined){ acc.textContent = s.feed_token==="set" ? "unknown" : "—"; acc.className="v"; }
  else if(dl < 0){ acc.textContent = "EXPIRED — send /bot on Telegram"; acc.className="v red"; }
  else { acc.textContent = "~"+Math.round(dl)+" day(s)"; acc.className = "v "+(dl<=3?"red":(dl<=7?"gold":"green")); }
  $("stSecret").textContent = s.api_secret;
  $("stSecret").className = "v "+(s.api_secret==="set"?"green":"");

  const pl=$("posList");
  if(s.open_positions && s.open_positions.length){
    pl.innerHTML = s.open_positions.map(p=>
      '<li><span>'+p.coin+'</span><span class="'+p.side+'">'+p.side.toUpperCase()+'</span></li>').join("");
  } else { pl.innerHTML='<li class="empty">none</li>'; }

  const lg=$("log");
  if(s.log && s.log.length){
    const atBottom = lg.scrollHeight - lg.scrollTop - lg.clientHeight < 40;
    lg.textContent = s.log.join("\n");
    if(atBottom) lg.scrollTop = lg.scrollHeight;
  }
}

async function poll(){
  try{ const r=await fetch("/api/status"); render(await r.json()); }catch(e){}
}

renderStep();
prefill();
poll();
setInterval(poll, 3000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------
def serve() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print("=" * 64)
    print("  The Observer — Auto-Execution Bot · local control panel")
    print(f"  Open this in your browser:  {url}")
    print("  (bound to 127.0.0.1 only — not reachable from your network)")
    print("  Press Ctrl-C here to shut the panel down.")
    print("=" * 64)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down the control panel…")
    finally:
        if _stop_event is not None:
            _stop_event.set()
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    serve()
