# Telegram signals to MT5

`personal_trade_bot.py` reads selected groups through **your own Telegram account** and can send recognized signals to a **local Windows x64 MT5 terminal**. No bot needs to be added to the groups. It defaults to paper mode, which records recognized signals without sending orders. `trade_bot.py` provides an optional Bot API reader; `copy_bot.py` copies between Telegram chats.

For Windows setup with local login prompts, follow [WINDOWS_SETUP.md](WINDOWS_SETUP.md).

## Personal Telegram account setup

1. On your own computer, visit [my.telegram.org](https://my.telegram.org), sign in, and open **API development tools** to obtain an API ID and API hash. Configure `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in your local process environment. The personal client does not need `TELEGRAM_BOT_TOKEN`.
2. Install the personal client dependencies and authorize on the Windows PC/VPS that will run MT5:

   ```powershell
   py -m pip install -r requirements-personal.txt -r requirements-mt5.txt
   py personal_trade_bot.py --login
   py personal_trade_bot.py --list-chats
   ```

   The login command prompts locally for your phone number, Telegram login code, and 2FA password if enabled. Never send them in chat. The read-only listing prints accessible group/channel names and numeric IDs; no groups are joined and no Telegram messages are sent.
3. Your selected source is **TheGoldenEdge V2**. Set `SOURCE_CHAT_NAME` to this exact name (as shown in `.env.example`), leaving `SOURCE_CHAT_IDS` empty. At startup, the client resolves the name to one accessible group/channel ID; missing or duplicate names stop startup. Alternatively, set `SOURCE_CHAT_IDS` to the numeric ID from `--list-chats` to select it independently of later renames. Nonempty numeric IDs take precedence over the name. Messages from every other chat, outgoing messages, service messages, and edits are excluded. New incoming signals posted by other Telegram bots are readable through your personal account.
4. Start paper mode, review recognized signals and their fixed-lot allocation, and verify exclusion of an unselected group:

   ```powershell
   py personal_trade_bot.py
   ```

5. For execution, log into the intended MT5 account in the desktop terminal, set `MT5_ACCOUNT_ID`, and use `py personal_trade_bot.py --execute`. The lot and target defaults below apply. A hedging account is required for separate target positions. First verify the full workflow against a demo terminal.

On Linux, paper mode uses `python -m pip install -r requirements-personal.txt` followed by `python personal_trade_bot.py`. In the prepared cloud environment, use `.venv/bin/python`. MT5 execution requires Windows. Telegram personal-account access uses MTProto directly to Telegram data centers, usually TCP port 443; an HTTPS allowlist for `api.telegram.org` alone does not provide that connection. Prefer running this client beside your Windows MT5 terminal.

The personal client keeps its authorization at `~/.local/state/copy-bot/telegram.session` by default; override with `TELEGRAM_SESSION_PATH` ending in `.session`. Keep this session outside the checkout and never commit, upload, publish in a cloud snapshot, or share it: it grants account access. Session filenames are ignored by Git as an additional safeguard. Restrict access to the session folder on your Windows PC/VPS. On POSIX systems, newly created session directories and files use private permissions. Revoke an unwanted authorization in Telegram **Settings → Devices**. Login is separate from normal startup; unattended runs never prompt for a login code.

Only new incoming messages are processed; no history is replayed. Incoming messages go through one sequential worker. Any submission failure stops that worker and disconnects Telegram rather than continuing with later trades. An existing journal prevents re-submitting already processed messages across client changes and restarts. Use one process per personal session and trading journal.

## MT5 workflow

Both Telegram clients share the same parser, journal, fixed sizing, and MT5 execution code. The commands in this section using `trade_bot.py` describe the optional Bot API workflow; for your personal-account workflow use `personal_trade_bot.py` and the setup above. The Bot API client requires adding a bot to source groups and cannot receive messages sent by other bots.

The parser accepts this compact format, with spaces or newlines between fields:

```text
BUY EURUSD ENTRY 1.1000 SL 1.0950 TP 1.1100
```

It also accepts this Telegram zone format, including optional leading emojis, blank lines, and a final risk line:

```text
💵XAUUSD BUY ZONE
Entry: 4172-69
Stoploss: 4166
TP1: 4182
TP2: 4198
TP3: 4207
TP4: OPEN
🛡Risk: 0.5-1%
```

Here `4172-69` expands to the entry zone **4169–4172**. All three numeric targets, the open target, and the risk range are preserved. `OPEN` means an unspecified target, not a zero price. Numbered targets must be consecutive and progress in the trade direction; OPEN can only be last. A stop loss must be beyond the entire zone. The parser also accepts full-price ranges and `SELL` zones. Use full prices for ambiguous shorthand.

Use the broker's exact symbol, including suffix and case. SL and at least one numeric TP are mandatory. Pending orders, images, edits, and trade-management instructions are not implemented. Unrecognized messages are ignored.

The configured sizing rule is **0.1 lot total per signal**, for buys and sells alike:

| Portion | Lots | Sample take profit |
| --- | --- | --- |
| TP1 | 0.05 | 4182 |
| TP2 | 0.03 | 4198 |
| TP3 | 0.02 | 4207 |

This uses all 0.1 lot, so the sample's `TP4: OPEN` receives no position. The message's risk percentage is recorded as metadata and **ignored for sizing**. The signal's stop loss is applied to every position. `MT5_LOTS` defaults to `0.1`, and `MT5_TARGET_LOTS` defaults to `0.05,0.03,0.02`. A single-target signal uses the entire 0.1 lot at that target. Other target counts need an explicit matching allocation; no extra targets are silently discarded. Custom allocations must sum to the total and meet broker minimum/step requirements; no volumes are rounded up or redistributed.

Each target is a separate position, requiring an **MT5 hedging account** for multi-target signals. Netting accounts are rejected for this workflow because their positions would merge. To intentionally keep an open portion under a different configuration, supply one additional lot size for OPEN; that portion has the same SL and no TP. The current chosen configuration has no open portion.

Parse the supplied sample without credentials or any Telegram/MT5 connection:

```sh
python trade_bot.py --parse-signal tests/fixtures/xau_buy.txt
# Or use --parse-signal - to read a pasted message from stdin.
```

Configure `TELEGRAM_BOT_TOKEN` and `SOURCE_CHAT_IDS` securely as environment variables, then run:

```sh
python trade_bot.py
```

Paper mode logs and journals the complete parsed signal without placing orders. It runs on Linux or Windows without third-party dependencies. This cloud environment is Linux; it cannot run the official MT5 Python connection. On your Windows x64 PC/VPS, with a supported 64-bit Python and MT5 terminal:

```powershell
py -m pip install -r requirements-mt5.txt
# Log into the intended account in the MT5 desktop terminal first.
# Configure TELEGRAM_BOT_TOKEN, SOURCE_CHAT_IDS and MT5_ACCOUNT_ID
# securely in the process environment. Do not put passwords in source code.
# MT5_LOTS=0.1 and MT5_TARGET_LOTS=0.05,0.03,0.02 are already the defaults.
py trade_bot.py
```

To explicitly enable automatic order placement, use `py trade_bot.py --execute` after configuring the intended account. Begin verification on a demo account. There is no per-signal approval queue, aggregate exposure limit, or daily-loss limit; execution mode automatically submits recognized eligible signals using the fixed lots above. Paper mode remains the default.

Execution requires a fresh signal (at most 120 seconds old), a quote at most 30 seconds old, current price inside the entry zone (or within 0.1% for a single entry price), valid SL/TP, supported per-position volume, fill-or-kill support, and successful MT5 `order_check` results for every portion before any order is sent. Account, quote, and order checks run again before each submission. These are market orders, not orders guaranteed to fill at the signal's entry. A zone message received while price is outside the zone is rejected; this version does not wait for a later entry. Broker checks can reject orders. Demo and live accounts both require the explicit `--execute` flag; no orders have been tested against a terminal in this cloud environment.

The SQLite journal at `~/.local/state/copy-bot/trades.sqlite3` prevents processing the same chat/message twice, including across restarts and paper-to-execution switches. Override with `TRADE_BOT_STATE`. Preserve this file and run one process per Telegram bot token; do not run `copy_bot.py` simultaneously with `trade_bot.py` using the same token. Orders are submitted sequentially, so the three-position batch is not atomic: a later portion can fail after an earlier one fills. A submission failure or uncertain outcome stops the process and leaves the signal `submitting`; no portion of that message is automatically retried. The `executions` table records each attempted order and its return code/order/deal IDs, when received. Inspect the MT5 terminal before taking action. Earlier positions are not automatically closed on failure. Journal storage is not encrypted and should remain private. Old recognized signals are ignored rather than traded after downtime.

Run `python -m unittest discover -s tests -v` for parser, allowlist, stale-signal, duplicate, and mocked broker checks. Live readiness additionally requires a demo-terminal delivery test and exclusion test with your actual Telegram groups. Never send account passwords or bot tokens in chat.

## Optional Telegram-to-Telegram copying

Copies new messages from an explicit list of Telegram groups or channels to one destination chat. All other sources are ignored. Uses Python 3.10+ and the standard library; no dependency installation is needed.

## Telegram setup

1. Create a bot with Telegram's @BotFather. Keep its token private.
2. Add the bot to each source group. Disable group privacy with @BotFather (and re-add the bot if necessary), or make it an administrator, so it receives ordinary group messages. For source channels, add it as an administrator. Bots cannot read messages sent by other bots.
3. Add the bot to the destination and give it permission to send messages. For a private destination, open the bot and send `/start` first.
4. Obtain the numeric source and destination chat IDs from Telegram updates, using a trusted local client. Group/channel IDs are commonly negative. Never paste a token into a public URL or chat.
5. Copy `.env.example` to `.env`, fill in the values, then run from this repository:

```sh
set -a
. ./.env
set +a
python copy_bot.py
```

Use only trusted values in `.env`, because the shell sources this file. Alternatively configure these environment variables directly through your environment settings. Never commit the real `.env`.

## Behavior and limits

- `SOURCE_CHAT_IDS` is a comma-separated numeric allowlist. No source is enabled by default. The destination cannot also be a source.
- Every new message in an allowed group is treated as a signal. This version does not classify message content, execute trades, copy history, or synchronize edits/deletions.
- Telegram's `copyMessage` preserves supported text and media. Albums are copied as individual messages. Protected content and some message types cannot be copied.
- Failed copies are retried and block later updates until resolved. A crash or uncertain network response can cause a duplicate; delivery is not exactly-once.
- Polling progress is stored at `~/.local/state/copy-bot/offset`; override with `COPY_BOT_STATE`. Keep one running instance per token and use a separate state path for each bot. Keep the state file across restarts. Telegram only retains pending updates for a limited time.
- Startup checks bot identity, absence of a webhook, and access to the destination. Actual send permissions require a live delivery check.
- The bot must be allowed into the source chats. Reading groups through a personal Telegram account requires a different Telegram client implementation.

## Validation

```sh
python -m unittest discover -s tests -v
```

For live verification, start the bot and send a test signal in one allowed group and a different group where the bot is present. Confirm only the allowed group's message arrives at the destination. Live verification requires your Telegram configuration and credentials.
