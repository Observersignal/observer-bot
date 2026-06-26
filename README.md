# The Observer — Auto-Execution Bot

Auto-execute The Observer's signals on **your own** Hyperliquid account.

## What this is

This bot runs **on your machine** (laptop, VPS — wherever you like). When a
signal fires, it places the matching order on **your own** Hyperliquid account
using **your own** keys.

It is **non-custodial**. We never touch your funds and we never see your keys:

- **Your keys.** They live only in your local `.env` file, on your machine.
- **Your account.** Orders are placed on your Hyperliquid wallet, by you.
- **Your risk.** You choose the size, the leverage and the limits.

We never have custody of, access to, or control over your money or your keys.
The bot connects out to the signal feed; nothing connects in to your machine.

## Prerequisites

1. **A Hyperliquid account** with funds on it.
2. **An API / agent wallet** for that account. This is the important part:

   On Hyperliquid you can create an **API wallet** (also called an agent
   wallet). It is a **trade-only** key:

   - it CAN place and cancel orders on your behalf, and
   - it **CANNOT withdraw or transfer your funds.**

   This is exactly the key you want to give an automated bot. Create it in the
   Hyperliquid interface (API / "API wallets" section), and copy its private
   key. **Never** use your main wallet's seed phrase or private key here.

3. **Python 3.9+** installed.

## Quick start (recommended) — the control panel

The easiest way to set up and run the bot is the local control panel. One
command opens a page in your browser where you fill in your settings with simple
forms and Start / Stop the bot with a button.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Launch the control panel
python app.py
```

Your browser opens to **http://127.0.0.1:8765** (the URL is also printed in the
console in case it doesn't open automatically). From there you:

1. **Fill in your settings** — capital, your Hyperliquid account, your feed
   token, risk and safety — in the form. Pressing **Save** writes them **only**
   to your local `.env`. Nothing is ever sent anywhere.
2. See a big **DRY-RUN / LIVE** indicator and press **Start / Stop**.
3. Watch **live status**: running state, open positions, realized-today, and the
   most recent log lines.

The panel is bound to **127.0.0.1 only** — it is never exposed on your network.
Your keys live only in your machine's `.env`; the page only ever shows whether a
secret is *set*, never its value. (Change the port with `UI_PORT=...` if 8765 is
taken.)

> Closing the browser tab does **not** stop the bot — it keeps running until you
> press **Stop** or quit `app.py` (Ctrl-C in its console).

### Headless / VPS option

If you prefer no UI (e.g. running 24-7 on a VPS), configure your `.env`
manually and run the loop directly — same engine, no browser:

```bash
# Copy the config template and edit it by hand
cp .env.example .env

# Run the loop until Ctrl-C
python bot.py
```

The fields you set are the same either way:

| Setting               | What to put                                              |
|-----------------------|---------------------------------------------------------|
| `OBSERVER_FEED_TOKEN` | The token you received with your subscription           |
| `HL_ACCOUNT_ADDRESS`  | Your main Hyperliquid wallet address (`0x...`)          |
| `HL_API_SECRET`       | Your **API / agent wallet** private key (trade-only)    |
| `BASE_CAPITAL`        | **Your total base capital (USD).** The bot sizes each trade as a proportion of this — same proportion as the model (1% per trade). Enter `5000` → $50/trade; `20000` → $200/trade. |
| `LEVERAGE`            | Leverage per trade (default 10)                         |
| `ISOLATED`            | Isolated margin (default true) — what the model uses    |
| `MAX_OPEN`            | Max positions open at once                              |
| `DAILY_LOSS_LIMIT_USD`| Stop opening new trades after this much daily loss      |

You only need to set `BASE_CAPITAL` — the per-trade size scales to your account automatically (no "cut").
Advanced: `RISK_PER_TRADE_PCT` (default 1.0%) tunes the proportion, and `SIZE_USD` can force a fixed margin.
All other settings have sensible defaults — see the comments in `.env.example`.

## Run a DRY-RUN first (recommended)

`DRY_RUN=true` is the default. In dry-run the bot **places no real orders** —
it just logs exactly what it *would* do. Start it from the panel (the **Start**
button while the DRY-RUN pill is amber), or headless:

```bash
python bot.py
```

If you have not set a feed token yet, the bot starts in **DEV MOCK MODE**: it
replays a short scripted sequence (open long BTC → open short ETH → close BTC →
flip ETH to long), one step per poll, so you can see the mechanics end-to-end
before anything is live. You will see lines like:

```
[DRY-RUN] OPEN LONG BTC | size=... | margin=100USD @ 10x | mark=...
[DRY-RUN] OPEN SHORT ETH | ...
[DRY-RUN] CLOSE BTC | ...
[DRY-RUN] FLIP ETH -> LONG | close then open | ...
```

Take your time here. Make sure the sizing and behaviour are what you expect.

## Go live

When you are confident, make sure your real feed token, account address and API
secret are set, then turn **DRY-RUN off**:

- **In the panel:** toggle **DRY-RUN** off (the pill turns green / **LIVE**),
  press **Save**, then **Start**. Going LIVE is blocked until those keys are set.
- **Headless:** set `DRY_RUN=false` in `.env` and run `python bot.py` again.

Now real orders are placed on your account.

## Kill-switch

To stop the bot from opening **new** trades immediately — without killing the
process — create a file named `STOP` in the bot's folder:

```bash
touch STOP
```

Delete it to resume:

```bash
rm STOP
```

To stop the bot entirely, press **Ctrl-C**. It saves its state and exits
cleanly, so you can restart it later without double-acting on past signals.

## Notes

- The bot keeps a small `state.json` locally so it never acts on the same
  signal twice, and to track open positions and your daily realized PnL.
- The bot only **trades**. There is no code anywhere in it to withdraw or move
  funds — by design.

---

## Disclaimer

This software is provided **"as is", without warranty of any kind**, express or
implied. It is **not financial, investment, trading or any other kind of
advice**.

- **You** control your sizing, leverage and risk limits.
- **You** are solely responsible for your account, your keys and every trade
  placed.
- Trading perpetual futures involves substantial risk, including the risk of
  losing more than you expect. **Past performance does not guarantee future
  results.**
- Signals may be delayed, wrong, or fail to execute. Markets can gap. Software,
  networks and exchanges can fail.
- We never take custody of, or have access to, your funds or your keys.

By running this software you accept full responsibility for its use and for any
outcomes on your account. If you do not accept this, do not run it. See
[`EULA.md`](./EULA.md).
