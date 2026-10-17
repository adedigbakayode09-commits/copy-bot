"""Forward Telegram messages from explicitly allowed source chats."""

import json
import logging
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        request = Request(self.base + method, data=json.dumps(params).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
        except HTTPError as error:
            # Never log the request URL: it contains the bot token.
            raise TelegramError(f"Telegram HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError):
            raise TelegramError("Telegram connection failed") from None
        if not result.get("ok"):
            raise TelegramError("Telegram rejected the request")
        return result["result"]


def settings(environ):
    token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN")
    try:
        sources = {int(value.strip()) for value in environ.get("SOURCE_CHAT_IDS", "").split(",") if value.strip()}
        destination = int(environ.get("DESTINATION_CHAT_ID", ""))
    except ValueError:
        raise ValueError("SOURCE_CHAT_IDS and DESTINATION_CHAT_ID must be numeric Telegram chat IDs") from None
    if not sources:
        raise ValueError("Set at least one SOURCE_CHAT_IDS entry")
    if destination in sources:
        raise ValueError("Destination must not be a source chat")
    return token, sources, destination


def copy_update(api, update, sources, destination):
    message = update.get("message") or update.get("channel_post")
    if not message or message.get("chat", {}).get("id") not in sources:
        return False
    api.call("copyMessage", chat_id=destination,
             from_chat_id=message["chat"]["id"], message_id=message["message_id"])
    return True


def save_offset(path, offset):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(str(offset))
    temporary.replace(path)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        token, sources, destination = settings(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    state = Path(os.environ.get("COPY_BOT_STATE", "~/.local/state/copy-bot/offset")).expanduser()
    offset = int(state.read_text()) if state.exists() else 0
    api = Telegram(token)
    # Verify identity and destination permissions without sending a message.
    try:
        api.call("getMe")
        if api.call("getWebhookInfo").get("url"):
            raise SystemExit("An active webhook exists; remove it before using polling")
        api.call("getChat", chat_id=destination)
    except TelegramError as error:
        raise SystemExit(str(error)) from None
    logging.info("Polling %d allowed source chats", len(sources))
    while True:
        try:
            updates = api.call("getUpdates", offset=offset, timeout=30,
                               allowed_updates=["message", "channel_post"])
            for update in updates:
                copy_update(api, update, sources, destination)
                # A failed copy must not acknowledge this or later updates.
                offset = update["update_id"] + 1
                save_offset(state, offset)
        except TelegramError as error:
            logging.warning("%s; retrying in 10 seconds", error)
            time.sleep(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
