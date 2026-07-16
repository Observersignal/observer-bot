"""Version of the client bot.

SINGLE SOURCE OF TRUTH for the bot's version. Two things read it:

  - `feed.py`, which sends it to the server as `X-Bot-Version` on every poll. The server uses it
    to decide which signal contract to serve: a bot that doesn't announce a version (or announces
    one below 1.1.0) gets the classic open/close feed instead of increase/decrease + units. That
    is what lets the server ship new contracts without breaking bots that haven't updated.
  - The packaging step on the server side, which stamps BOT_ZIP_VERSION in botZipData.ts from
    this value, so the published zip and the code inside it can never disagree.

Bump it in the same commit as any change to how the bot reads the feed or places orders.
"""

__version__ = "1.1.2"
