"""Selected Telegram chats -> strict signals -> paper mode or a local MT5 terminal."""

import argparse
from dataclasses import asdict, dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from copy_bot import Telegram, TelegramError


@dataclass(frozen=True)
class Signal:
    side: str
    symbol: str
    entry_low: float
    entry_high: float
    sl: float
    targets: tuple[float, ...]
    open_target: bool = False
    risk_percent: tuple[float, float] | None = None

    @property
    def entry(self):
        return self.entry_high

    @property
    def tp(self):
        return self.targets[0]


NUMBER = r"[0-9]+(?:\.[0-9]+)?"


def entry_zone(value):
    parts = re.split(r"\s*[-–]\s*", value)
    if len(parts) == 1:
        return float(parts[0]), float(parts[0])
    first, last = parts
    # Telegram shorthand: 4172-69 means 4172-4169; 1.1050-40 means 1.1050-1.1040.
    if "." not in last and len(last) < len(first.split(".")[0]):
        if "." in first:
            raise ValueError("Use full prices for ambiguous decimal entry ranges")
        last = first[:-len(last)] + last
    elif "." in first and "." not in last and len(last) <= len(first.split(".")[1]):
        last = first[:-len(last)] + last
    return tuple(sorted((float(first), float(last))))


def validate_signal(signal):
    prices = (signal.entry_low, signal.entry_high, signal.sl, *signal.targets)
    if not signal.targets or not all(math.isfinite(value) and value > 0 for value in prices):
        raise ValueError("Prices must be finite and positive with at least one numeric target")
    valid = (signal.sl < signal.entry_low <= signal.entry_high < signal.tp if signal.side == "BUY"
             else signal.tp < signal.entry_low <= signal.entry_high < signal.sl)
    if not valid:
        raise ValueError("Stop loss and take profit must surround the entire entry zone")
    if any((left >= right if signal.side == "BUY" else left <= right)
           for left, right in zip(signal.targets, signal.targets[1:])):
        raise ValueError("Numbered targets must progress in the trade direction")
    if signal.risk_percent is not None:
        low, high = signal.risk_percent
        if not 0 < low <= high <= 100:
            raise ValueError("Risk percentage must be positive, ordered, and no greater than 100")
    return signal


def parse_zone_signal(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 4:
        raise ValueError("Incomplete zone signal")
    header = re.fullmatch(r"[^\w]*([A-Z][A-Z0-9._]{0,29})\s+(BUY|SELL)\s+ZONE", lines[0], re.I)
    entry = re.fullmatch(r"Entry\s*:\s*(" + NUMBER + r"(?:\s*[-–]\s*" + NUMBER + r")?)", lines[1], re.I)
    stop = re.fullmatch(r"(?:Stoploss|Stop\s+loss|SL)\s*:\s*(" + NUMBER + r")", lines[2], re.I)
    if not header or not entry or not stop:
        raise ValueError("Expected SYMBOL BUY|SELL ZONE, Entry, and Stoploss lines")
    low, high = entry_zone(entry[1])
    targets, open_target, risk = [], False, None
    remaining = lines[3:]
    risk_match = re.fullmatch(r"[^\w]*Risk\s*:\s*(" + NUMBER + r")(?:\s*[-–]\s*(" + NUMBER + r"))?\s*%", remaining[-1], re.I)
    if risk_match:
        risk = (float(risk_match[1]), float(risk_match[2] or risk_match[1]))
        remaining = remaining[:-1]
    for index, line in enumerate(remaining, 1):
        target = re.fullmatch(r"TP([1-9][0-9]*)\s*:\s*(" + NUMBER + r"|OPEN)", line, re.I)
        if not target or int(target[1]) != index or open_target:
            raise ValueError("Expected consecutive TP1, TP2, ... lines, with OPEN only last")
        if target[2].upper() == "OPEN":
            open_target = True
        else:
            targets.append(float(target[2]))
    return validate_signal(Signal(header[2].upper(), header[1], low, high,
                                  float(stop[1]), tuple(targets), open_target, risk))


def parse_signal(text):
    """Accept one unambiguous market signal; never guess free-form instructions."""
    number = "(" + NUMBER + ")"
    pattern = (r"\s*(BUY|SELL)\s+([A-Z][A-Z0-9._]{0,29})\s+"
               r"ENTRY\s+" + number + r"\s+SL\s+" + number + r"\s+TP\s+" + number + r"\s*")
    match = re.fullmatch(pattern, text, re.IGNORECASE)
    if not match:
        return parse_zone_signal(text)
    side, symbol, entry, sl, tp = match.groups()
    return validate_signal(Signal(side.upper(), symbol, float(entry), float(entry), float(sl), (float(tp),)))


def read_sources(environ):
    try:
        sources = {int(x.strip()) for x in environ.get("SOURCE_CHAT_IDS", "").split(",") if x.strip()}
    except ValueError:
        raise ValueError("SOURCE_CHAT_IDS must contain numeric chat IDs") from None
    if not sources:
        raise ValueError("Set SOURCE_CHAT_IDS")
    return sources


def read_config(environ):
    token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN")
    return token, read_sources(environ)


@dataclass(frozen=True)
class ExecutionPolicy:
    total_lots: float = 0.1
    target_lots: tuple[float, ...] | None = (0.05, 0.03, 0.02)

    def __post_init__(self):
        if not math.isfinite(self.total_lots) or self.total_lots <= 0:
            raise ValueError("MT5_LOTS must be finite and positive")
        if self.target_lots is not None:
            if not self.target_lots or any(not math.isfinite(x) or x <= 0 for x in self.target_lots):
                raise ValueError("MT5_TARGET_LOTS must contain positive finite lot sizes")
            if not math.isclose(sum(self.target_lots), self.total_lots, rel_tol=0, abs_tol=1e-10):
                raise ValueError("MT5_TARGET_LOTS must sum to MT5_LOTS")

    def allocations(self, signal):
        targets = signal.targets + ((0.0,) if signal.open_target else ())
        if len(targets) == 1:
            return ((self.total_lots, targets[0]),)
        if signal.open_target and self.target_lots is not None and len(self.target_lots) == len(signal.targets):
            # A complete numeric-target allocation leaves no volume for OPEN.
            targets = signal.targets
        if self.target_lots is None or len(self.target_lots) != len(targets):
            raise ValueError("Set MT5_TARGET_LOTS with one lot size per TP, then OPEN if present")
        # The signal's risk percentage is metadata only: sizing is always fixed lots.
        return tuple(zip(self.target_lots, targets))


def execution_policy(environ):
    split = environ.get("MT5_TARGET_LOTS", "0.05,0.03,0.02").strip()
    return ExecutionPolicy(float(environ.get("MT5_LOTS", "0.1")),
                           tuple(float(x.strip()) for x in split.split(",")) if split else None)


@dataclass(frozen=True)
class PreparedTrade:
    signal: Signal
    requests: tuple[dict, ...]


class EntryNotReady(ValueError):
    """Valid signal whose current quote has not reached its entry zone."""


def entry_wait_seconds(environ):
    try:
        seconds = int(environ.get("ENTRY_WAIT_SECONDS", "900"))
    except ValueError:
        raise ValueError("ENTRY_WAIT_SECONDS must be an integer") from None
    if not 1 <= seconds <= 86400:
        raise ValueError("ENTRY_WAIT_SECONDS must be between 1 and 86400")
    return seconds


def entry_instruction(text):
    """Recognize explicit entry controls; never infer permission from ordinary chat."""
    text = " ".join(text.casefold().replace("’", "'").split())
    stop = (r"\b(?:don'?t|do not|never)\s+(?:enter|take\s+(?:the\s+)?(?:trade|entry)|open\s+(?:new\s+)?(?:trades|positions))\b",
            r"\b(?:no more|no new)\s+(?:entries|entry|trades)\b",
            r"\b(?:cancel|pause|stop)\s+(?:(?:all|new|the|this|pending|buy|sell)\s+)*(?:entries|entry|orders|trading|limits|setup)\b",
            r"\b(?:setup|entry|entries)\s+(?:(?:is|are)\s+)?cancel(?:led|ed)\b",
            r"\bstay out\b")
    if any(re.search(pattern, text) for pattern in stop):
        return "pause"
    if re.fullmatch(r"(?:resume (?:entries|trading)|entries resumed|you can enter (?:now|again)|enter again)[.! ]*", text):
        return "resume"
    return None


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS messages (chat INTEGER, message INTEGER, status TEXT, PRIMARY KEY(chat, message))")
        self.db.execute("CREATE TABLE IF NOT EXISTS signals (chat INTEGER, message INTEGER, payload TEXT, PRIMARY KEY(chat, message))")
        self.db.execute("CREATE TABLE IF NOT EXISTS executions (chat INTEGER, message INTEGER, leg INTEGER, request TEXT, result TEXT, PRIMARY KEY(chat, message, leg))")
        self.db.execute("CREATE TABLE IF NOT EXISTS entry_state (chat INTEGER, topic INTEGER, paused INTEGER, stamp REAL, message INTEGER, PRIMARY KEY(chat, topic))")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (chat INTEGER, topic INTEGER, message INTEGER, stamp REAL, command TEXT, PRIMARY KEY(chat, topic, message, stamp, command))")
        self.db.execute("CREATE TABLE IF NOT EXISTS observations (chat INTEGER, topic INTEGER, message INTEGER, stamp REAL, edited INTEGER, text TEXT, status TEXT, PRIMARY KEY(chat, topic, message, stamp, edited))")
        self.db.execute("CREATE TABLE IF NOT EXISTS waiting_entries (chat INTEGER, topic INTEGER, message INTEGER, signal TEXT, expires REAL, status TEXT, PRIMARY KEY(chat, topic, message))")
        self.db.commit()

    def claim(self, chat, message, status, signal=None):
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?)", (chat, message, status))
            if cursor.rowcount and signal is not None:
                self.db.execute("INSERT INTO signals VALUES (?, ?, ?)", (chat, message, json.dumps(asdict(signal))))
        return cursor.rowcount == 1

    def finish(self, chat, message, status):
        with self.db:
            self.db.execute("UPDATE messages SET status=? WHERE chat=? AND message=?", (status, chat, message))

    def record_order(self, chat, message, leg, request, result):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO executions VALUES (?, ?, ?, ?, ?)",
                            (chat, message, leg, json.dumps(request), json.dumps(result)))

    def observe(self, message, status):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO observations VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (message["chat"]["id"], message.get("message_thread_id", 0), message["message_id"],
                             message.get("date", 0), int(message.get("is_edit", False)),
                             message.get("text") or message.get("caption") or "", status))

    def entries_paused(self, chat, topic):
        row = self.db.execute("SELECT paused FROM entry_state WHERE chat=? AND topic=?", (chat, topic)).fetchone()
        return bool(row and row[0])

    def apply_control(self, message, command):
        chat, topic = message["chat"]["id"], message.get("message_thread_id", 0)
        stamp, message_id = message["date"], message["message_id"]
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO controls VALUES (?, ?, ?, ?, ?)",
                                     (chat, topic, message_id, stamp, command))
            if not cursor.rowcount:
                return "duplicate-control"
            previous = self.db.execute("SELECT stamp, message FROM entry_state WHERE chat=? AND topic=?", (chat, topic)).fetchone()
            if previous and (stamp, message_id) < previous:
                return "outdated-control"
            self.db.execute("INSERT OR REPLACE INTO entry_state VALUES (?, ?, ?, ?, ?)",
                            (chat, topic, int(command == "pause"), stamp, message_id))
            if command == "pause":
                self.cancel_waiting(chat, topic, "cancelled")
        return "entries-paused" if command == "pause" else "entries-resumed"

    def cancel_waiting(self, chat, topic, status):
        self.db.execute("UPDATE messages SET status=? WHERE chat=? AND message IN (SELECT message FROM waiting_entries WHERE chat=? AND topic=? AND status='waiting')",
                        (status, chat, chat, topic))
        self.db.execute("UPDATE waiting_entries SET status=? WHERE chat=? AND topic=? AND status='waiting'", (status, chat, topic))

    def wait_for_entry(self, message, signal, seconds):
        chat, topic = message["chat"]["id"], message.get("message_thread_id", 0)
        with self.db:
            self.cancel_waiting(chat, topic, "superseded")
            self.db.execute("INSERT INTO waiting_entries VALUES (?, ?, ?, ?, ?, 'waiting')",
                            (chat, topic, message["message_id"], json.dumps(asdict(signal)), message["date"] + seconds))
            self.db.execute("UPDATE messages SET status='waiting' WHERE chat=? AND message=?", (chat, message["message_id"]))

    def finish_waiting(self, chat, topic, message, status):
        with self.db:
            self.db.execute("UPDATE waiting_entries SET status=? WHERE chat=? AND topic=? AND message=?", (status, chat, topic, message))
            self.db.execute("UPDATE messages SET status=? WHERE chat=? AND message=?", (status, chat, message))

    def interrupt_waiting(self):
        # A stop instruction may have been posted while disconnected. Do not
        # resume an old setup after a crash or a disconnected session.
        with self.db:
            scopes = self.db.execute("SELECT DISTINCT chat, topic FROM waiting_entries WHERE status='waiting'").fetchall()
            for chat, topic in scopes:
                self.cancel_waiting(chat, topic, "interrupted")


class MT5Broker:
    def __init__(self, module, expected_account, lots=0.1, target_lots=None):
        self.policy = ExecutionPolicy(lots, target_lots)
        self.mt5, self.account = module, expected_account
        if not module.initialize():
            raise RuntimeError("Cannot connect to local MT5 terminal")
        self.check_account()

    def check_account(self):
        account = self.mt5.account_info()
        terminal = self.mt5.terminal_info()
        if account is None or account.login != self.account:
            raise RuntimeError("MT5 account does not match MT5_ACCOUNT_ID")
        if not terminal or not terminal.connected or not terminal.trade_allowed or not account.trade_allowed or not account.trade_expert:
            raise RuntimeError("MT5 terminal/account does not allow automated trading")
        return account

    def quote(self, signal):
        info, tick = self.mt5.symbol_info(signal.symbol), self.mt5.symbol_info_tick(signal.symbol)
        if info is None or tick is None or info.point <= 0 or info.volume_step <= 0:
            raise ValueError("No usable symbol information or quote")
        if not 0 <= time.time() - tick.time <= 30:
            raise ValueError("Quote is stale")
        price = tick.ask if signal.side == "BUY" else tick.bid
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Market price is invalid")
        if signal.entry_low != signal.entry_high:
            if not signal.entry_low <= price <= signal.entry_high:
                raise EntryNotReady("Market price is outside the signal entry zone")
        elif abs(price - signal.entry) / signal.entry > 0.001:
            raise EntryNotReady("Market price is more than 0.1% from signal entry")
        if not (signal.sl < price < signal.tp if signal.side == "BUY" else signal.tp < price < signal.sl):
            raise ValueError("Current price is outside SL/TP")
        if not info.filling_mode & 1:
            raise ValueError("Symbol does not support fill-or-kill execution")
        return info, price

    def check_hedging(self, count):
        account = self.check_account()
        if count > 1 and account.margin_mode != self.mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError("Multiple TP positions require an MT5 hedging account; netting is unsupported")

    def check_request(self, request, info):
        lots = request["volume"]
        if not info.volume_min <= lots <= info.volume_max:
            raise ValueError("Target lot size is outside the broker's allowed range")
        steps = lots / info.volume_step
        if not math.isclose(steps, round(steps), rel_tol=0, abs_tol=1e-7):
            raise ValueError("Target lot size does not match the broker's volume step")
        check = self.mt5.order_check(request)
        if check is None or check.retcode != 0:
            raise ValueError("MT5 order_check rejected the order")

    def prepare(self, signal):
        allocations = self.policy.allocations(signal)
        self.check_hedging(len(allocations))
        mt5 = self.mt5
        if not mt5.symbol_select(signal.symbol, True):
            raise ValueError("Symbol unavailable; use the broker's exact symbol")
        info, price = self.quote(signal)
        requests = []
        for leg, (lots, target) in enumerate(allocations, 1):
            request = dict(action=mt5.TRADE_ACTION_DEAL, symbol=signal.symbol, volume=lots,
                           type=mt5.ORDER_TYPE_BUY if signal.side == "BUY" else mt5.ORDER_TYPE_SELL,
                           price=price, sl=signal.sl, tp=target, deviation=10,
                           magic=745301, comment=f"telegram-copy-{leg}", type_time=mt5.ORDER_TIME_GTC,
                           type_filling=mt5.ORDER_FILLING_FOK)
            # Check every leg before sending the first; never round volumes up.
            self.check_request(request, info)
            requests.append(request)
        return PreparedTrade(signal, tuple(requests))

    def execute(self, plan, record=None):
        results = []
        for leg, request in enumerate(plan.requests, 1):
            self.check_hedging(len(plan.requests))
            info, price = self.quote(plan.signal)
            request = dict(request, price=price)
            self.check_request(request, info)
            if record:
                record(leg, request, None)
            result = self.mt5.order_send(request)
            details = None if result is None else {"retcode": result.retcode, "order": result.order, "deal": result.deal}
            if record:
                record(leg, request, details)
            if result is None or result.retcode != self.mt5.TRADE_RETCODE_DONE:
                raise RuntimeError("MT5 order result is rejected or uncertain; inspect terminal before taking action")
            results.append(result)
        return results


def process_update(update, sources, journal, broker=None, now=None, policy=None, execute_orders=True, wait_seconds=900):
    message = update.get("message") or update.get("channel_post")
    if not message or message.get("chat", {}).get("id") not in sources:
        return "ignored"

    def report(status):
        journal.observe(message, status)
        return status

    journal.observe(message, "received")
    now = time.time() if now is None else now
    timestamp = message.get("date", 0)
    if not isinstance(timestamp, (int, float)) or not 0 <= now - timestamp <= 120:
        return report("stale")
    text = message.get("text") or message.get("caption") or ""
    instruction = entry_instruction(text)
    if instruction == "pause" or (instruction == "resume" and not message.get("is_edit")):
        return report(journal.apply_control(message, instruction))
    if message.get("is_edit"):
        return report("edited-ignored")
    try:
        signal = parse_signal(text)
    except ValueError:
        return report("unrecognized")
    chat, message_id = message["chat"]["id"], message["message_id"]
    # Persist intent before sending. An uncertain order is NEVER retried automatically.
    if not journal.claim(chat, message_id, "preparing" if broker else "paper", signal):
        return report("duplicate")
    if journal.entries_paused(chat, message.get("message_thread_id", 0)):
        journal.finish(chat, message_id, "blocked-paused")
        return report("blocked-paused")
    with journal.db:
        journal.cancel_waiting(chat, message.get("message_thread_id", 0), "superseded")
    if broker:
        try:
            request = broker.prepare(signal)
        except EntryNotReady:
            journal.wait_for_entry(message, signal, wait_seconds)
            return report("waiting")
        except ValueError:
            journal.finish(chat, message_id, "rejected")
            return report("rejected")
        if execute_orders:
            journal.finish(chat, message_id, "submitting")
            broker.execute(request, record=lambda leg, request, result: journal.record_order(chat, message_id, leg, request, result))
            journal.finish(chat, message_id, "executed")
            return report("executed")
        journal.finish(chat, message_id, "paper-ready")
        return report("paper-ready")
    logging.info("Paper signal: %s", json.dumps(asdict(signal)))
    try:
        allocations = (policy or ExecutionPolicy()).allocations(signal)
        logging.info("Paper fixed-lot allocation (risk %% ignored): %s", allocations)
    except ValueError as error:
        logging.info("Paper allocation pending: %s", error)
    return report("paper")


def process_waiting(journal, broker, sources, topic_id=None, now=None, execute_orders=True):
    if broker is None:
        return
    now = time.time() if now is None else now
    rows = journal.db.execute("SELECT chat, topic, message, signal, expires FROM waiting_entries WHERE status='waiting' ORDER BY message").fetchall()
    for chat, topic, message, payload, expires in rows:
        if chat not in sources or (topic_id is not None and topic != topic_id):
            continue
        if journal.entries_paused(chat, topic):
            journal.finish_waiting(chat, topic, message, "cancelled")
            continue
        if now >= expires:
            journal.finish_waiting(chat, topic, message, "expired")
            logging.info("Waiting setup %s expired", message)
            continue
        data = json.loads(payload)
        data["targets"] = tuple(data["targets"])
        if data["risk_percent"] is not None:
            data["risk_percent"] = tuple(data["risk_percent"])
        try:
            plan = broker.prepare(Signal(**data))
        except EntryNotReady:
            continue
        except ValueError:
            journal.finish_waiting(chat, topic, message, "rejected")
            continue
        journal.finish_waiting(chat, topic, message, "submitting" if execute_orders else "paper-ready")
        if execute_orders:
            broker.execute(plan, record=lambda leg, request, result: journal.record_order(chat, message, leg, request, result))
            journal.finish_waiting(chat, topic, message, "executed")
        logging.info("Waiting setup %s: %s", message, "executed" if execute_orders else "paper-ready")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Place real orders through a local Windows MT5 terminal")
    mode.add_argument("--parse-signal", metavar="FILE", help="Parse a local signal file (or - for stdin); no credentials or orders")
    args = parser.parse_args()
    if args.parse_signal:
        try:
            text = sys.stdin.read() if args.parse_signal == "-" else Path(args.parse_signal).read_text(encoding="utf-8")
            print(json.dumps(asdict(parse_signal(text)), indent=2))
        except (ValueError, OSError) as error:
            parser.error(str(error))
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    broker = None
    try:
        token, sources = read_config(os.environ)
        policy = execution_policy(os.environ)
        wait_seconds = entry_wait_seconds(os.environ)
        if args.execute:
            import MetaTrader5 as mt5
            broker = MT5Broker(mt5, int(os.environ["MT5_ACCOUNT_ID"]), policy.total_lots, policy.target_lots)
    except (ValueError, KeyError, ImportError, RuntimeError) as error:
        raise SystemExit(f"Startup failed: {error}") from None
    path = Path(os.environ.get("TRADE_BOT_STATE", "~/.local/state/copy-bot/trades.sqlite3")).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(path)
    journal.interrupt_waiting()
    api = Telegram(token)
    api.call("getMe")
    if api.call("getWebhookInfo").get("url"):
        raise SystemExit("Remove the active webhook before using polling")
    logging.info("Starting %s mode", "EXECUTION" if broker else "PAPER")
    offset = 0
    try:
        while True:
            try:
                waiting = journal.db.execute("SELECT 1 FROM waiting_entries WHERE status='waiting' LIMIT 1").fetchone()
                updates = api.call("getUpdates", offset=offset, timeout=2 if waiting else 30,
                                   allowed_updates=["message", "channel_post"])
            except TelegramError as error:
                logging.warning("%s; retrying in 10 seconds", error)
                time.sleep(10)
                continue
            for update in updates:
                status = process_update(update, sources, journal, broker, policy=policy, wait_seconds=wait_seconds)
                logging.info("Update %s: %s", update["update_id"], status)
                offset = update["update_id"] + 1
            process_waiting(journal, broker, sources)
    finally:
        journal.interrupt_waiting()
        journal.db.close()
        if broker:
            broker.mt5.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
