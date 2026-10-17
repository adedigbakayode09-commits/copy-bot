"""Read selected groups with your own Telegram account; paper mode by default."""

import argparse
import asyncio
from contextlib import suppress
import getpass
import logging
import os
from pathlib import Path

from trade_bot import Journal, MT5Broker, execution_policy, process_update, read_sources


def personal_config(environ):
    try:
        api_id = int(environ.get("TELEGRAM_API_ID", ""))
    except ValueError:
        raise ValueError("Set TELEGRAM_API_ID to the numeric API ID from my.telegram.org") from None
    api_hash = environ.get("TELEGRAM_API_HASH", "").strip()
    if api_id <= 0 or not api_hash:
        raise ValueError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH from my.telegram.org")
    session = Path(environ.get("TELEGRAM_SESSION_PATH", "~/.local/state/copy-bot/telegram.session")).expanduser()
    if session.suffix != ".session":
        raise ValueError("TELEGRAM_SESSION_PATH must end in .session")
    return api_id, api_hash, session


def normalize_event(event, sources):
    """Normalize only new incoming group/channel messages from the allowlist."""
    if event.chat_id not in sources or event.message.out or event.message.action:
        return None
    if not (event.is_group or event.is_channel):
        return None
    return {"message": {"chat": {"id": event.chat_id}, "message_id": event.message.id,
                        "date": event.message.date.timestamp(), "text": event.raw_text or ""}}


def resolve_sources(environ, dialogs):
    available = {d.id for d in dialogs if d.is_group or d.is_channel}
    if environ.get("SOURCE_CHAT_IDS", "").strip():
        sources = read_sources(environ)
        if not sources <= available:
            raise ValueError("Some SOURCE_CHAT_IDS are not accessible groups/channels; use --list-chats")
        return sources
    name = environ.get("SOURCE_CHAT_NAME", "").strip()
    if not name:
        raise ValueError("Set SOURCE_CHAT_NAME or numeric SOURCE_CHAT_IDS")
    matches = {d.id for d in dialogs if d.id in available and d.name == name}
    if not matches:
        raise ValueError("SOURCE_CHAT_NAME has no exact accessible group/channel match; use --list-chats")
    if len(matches) != 1:
        raise ValueError("SOURCE_CHAT_NAME is ambiguous; select the correct numeric SOURCE_CHAT_IDS using --list-chats")
    return matches


async def consume(queue, sources, journal, broker, policy):
    while True:
        update = await queue.get()
        try:
            status = process_update(update, sources, journal, broker, policy=policy)
            logging.info("Source %s message %s: %s", update["message"]["chat"]["id"],
                         update["message"]["message_id"], status)
        finally:
            queue.task_done()
        # Keep sequential order submissions and let Telegram process network events.
        await asyncio.sleep(0)


async def run(args):
    from telethon import TelegramClient, events

    api_id, api_hash, session = personal_config(os.environ)
    policy = execution_policy(os.environ)
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = TelegramClient(str(session), api_id, api_hash, sequential_updates=True)
    broker = journal = worker = None
    try:
        await client.connect()
        if os.name != "nt" and session.exists():
            session.chmod(0o600)
        if args.login:
            await client.start(phone=lambda: input("Telegram phone number (local only): "),
                               code_callback=lambda: getpass.getpass("Telegram login code (hidden): "),
                               password=lambda: getpass.getpass("Telegram 2FA password (hidden): "))
            if (await client.get_me()).bot:
                raise ValueError("A personal Telegram account is required")
            print("Personal Telegram session saved locally. Keep the session file private.")
            return
        if not await client.is_user_authorized():
            raise ValueError("No authorized personal session. Run personal_trade_bot.py --login locally first")
        if (await client.get_me()).bot:
            raise ValueError("A personal Telegram account is required")
        dialogs = await client.get_dialogs()
        available = {d.id for d in dialogs if d.is_group or d.is_channel}
        if args.list_chats:
            for dialog in dialogs:
                if dialog.id in available:
                    print(f"{dialog.id}\t{dialog.name}")
            return
        sources = resolve_sources(os.environ, dialogs)
        if args.execute:
            import MetaTrader5 as mt5
            broker = MT5Broker(mt5, int(os.environ["MT5_ACCOUNT_ID"]), policy.total_lots, policy.target_lots)
        state = Path(os.environ.get("TRADE_BOT_STATE", "~/.local/state/copy-bot/trades.sqlite3")).expanduser()
        state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        journal = Journal(state)
        queue = asyncio.Queue(maxsize=1000)

        async def on_message(event):
            update = normalize_event(event, sources)
            if update is not None:
                await queue.put(update)

        client.add_event_handler(on_message, events.NewMessage(incoming=True, chats=list(sources)))
        logging.info("Personal account listening to %d allowed groups in %s mode", len(sources),
                     "EXECUTION" if broker else "PAPER")
        worker = asyncio.create_task(consume(queue, sources, journal, broker, policy))
        done, _ = await asyncio.wait((worker, client.disconnected), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # Propagate submission failures; do not silently keep trading.
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await worker
        await client.disconnect()
        if journal:
            journal.db.close()
        if broker:
            broker.mt5.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--login", action="store_true", help="Authorize locally using Telegram login prompts")
    mode.add_argument("--list-chats", action="store_true", help="List accessible groups/channels and numeric IDs")
    mode.add_argument("--execute", action="store_true", help="Place orders through the local Windows MT5 terminal")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except (ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
    except ImportError:
        raise SystemExit("Install requirements-personal.txt (and requirements-mt5.txt for Windows execution)") from None
    except KeyboardInterrupt:
        pass
    except Exception:
        # Authentication errors may contain private account data; do not print them.
        raise SystemExit("Personal client stopped. Check local Telegram access/network and MT5/journal state before restarting.") from None


if __name__ == "__main__":
    main()
