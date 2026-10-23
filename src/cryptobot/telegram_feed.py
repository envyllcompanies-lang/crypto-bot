"""Telegram public channels, read through Telegram's official user API (MTProto, via
Telethon), for social.py. Off until the owner sets it up, and read-only.

Risk, for the owner to weigh first: Telegram allows third-party clients, but accounts
that log in through them can be put under observation or banned, and its terms added a
data-scraping clause in Feb 2026. Use an account you can afford to lose, read only a
few channels you would follow anyway, and never post.

Setup (on the server; nothing goes in chat):

1. Get an api_id and api_hash at https://my.telegram.org -> API development tools.
2. Add to `.env`:

       TELEGRAM_API_ID=...
       TELEGRAM_API_HASH=...
       TELEGRAM_CHANNELS=channel_one,channel_two     # public @usernames, without the @

3. Log in once, interactively (it asks for your phone number and the code Telegram
   sends):  .venv/bin/python -m cryptobot.telegram_feed login
   This writes data/derived/telegram.session, which is a login credential: it is
   git-ignored and must never be shared.
4. sudo systemctl restart crypto-bot-social

social.py then counts mint addresses and $TICKERs in new messages from those channels
over the last hour (`tg_mint`, `tg_ticker`), so a coin "called" in a channel shows up
next to its outcome.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from cryptobot.recorder import LIVE

SESSION = LIVE.parent / "derived" / "telegram.session"


def configured() -> bool:
    return (
        all(
            os.environ.get(k) for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_CHANNELS")
        )
        and SESSION.exists()
    )


def _client():
    from telethon import TelegramClient

    return TelegramClient(
        str(SESSION.with_suffix("")), int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )  # fmt: skip


def start(on_text, channels: list[str] | None = None) -> None:
    """Listen to the channels in a background thread; on_text(text, ms) for each message."""
    import asyncio

    from telethon import events

    names = channels or [c.strip() for c in os.environ["TELEGRAM_CHANNELS"].split(",") if c]

    async def listen() -> None:
        while True:
            try:
                client = _client()
                await client.connect()
                if not await client.is_user_authorized():
                    print("telegram: not logged in; run `telegram_feed login`", file=sys.stderr)
                    return

                @client.on(events.NewMessage(chats=names))
                async def handler(event) -> None:
                    on_text(event.raw_text or "", int(time.time() * 1000))

                print(f"telegram: listening to {len(names)} channels", flush=True)
                await client.run_until_disconnected()
            except Exception as e:
                print(f"telegram: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                await asyncio.sleep(60)

    threading.Thread(target=lambda: asyncio.run(listen()), daemon=True).start()


def login(session: Path = SESSION) -> None:
    session.parent.mkdir(parents=True, exist_ok=True)
    client = _client()
    client.start()  # asks for the phone number and code on the terminal
    me = client.get_me()
    print(f"logged in as {getattr(me, 'username', None) or me.id}; session saved")
    client.disconnect()


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        login()
    else:
        print("usage: python -m cryptobot.telegram_feed login")
