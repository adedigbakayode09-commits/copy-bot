# Connect your Telegram account on Windows

Authorization gives the local app access to Telegram on your PC. This chat cannot browse that local account. Keep the login code, 2FA password, API hash, and `.session` file private.

## 1. Prepare the folder

Install **64-bit Python 3.12** from [python.org](https://www.python.org/downloads/windows/) with the Python launcher. Extract the supplied ZIP. Open the extracted `copy-bot` folder, right-click an empty area, and choose **Open in Terminal**. Use a PowerShell tab.

Run these commands from the folder containing `personal_trade_bot.py`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-personal.txt
```

## 2. Get your Telegram API credentials

On your own PC, open [my.telegram.org](https://my.telegram.org), sign in, and choose **API development tools**. Create an application if requested. Copy its numeric **API ID** and **API hash** into the local prompts below. Do not send those values in chat.

In the same PowerShell window:

```powershell
$env:TELEGRAM_API_ID = Read-Host "Telegram API ID"
$env:TELEGRAM_API_HASH = [System.Net.NetworkCredential]::new("", (Read-Host "Telegram API hash" -AsSecureString)).Password
$env:SOURCE_CHAT_NAME = "TheGoldenEdgeVIP"
$env:SOURCE_CHAT_IDS = "-1004418162453"
$env:SOURCE_TOPIC_ID = "5"
.\.venv\Scripts\python.exe personal_trade_bot.py --login
```

Enter your phone number, login code received through Telegram, and your Telegram 2FA password if prompted. The code and password are hidden as you enter them. Enter them only in this local terminal.

The app saves an account session at `%USERPROFILE%\.local\state\copy-bot\telegram.session`. Keep it on this PC and outside the project folder. Never upload it or include it in a cloud snapshot. You can revoke the session through Telegram **Settings → Devices**.

## 3. Check TheGoldenEdge V2 in paper mode

In the same PowerShell window:

```powershell
.\.venv\Scripts\python.exe personal_trade_bot.py
```

It should report that it is listening to one allowed group in **PAPER** mode and show **Source topic: 5**. This is TheGoldenEdge V2 inside TheGoldenEdgeVIP, not the group's other topics. It listens to new incoming messages only; it does not replay old signals. When a new recognized signal arrives, it logs the parsed signal and the selected lot allocation. Stop phrases such as `don't enter` pause fresh entries and cancel locally waiting setups; `resume entries` allows fresh signals again. The pause survives restart. Edits can pause but cannot open trades or resume. No orders are sent in this mode. Press **Ctrl+C** to stop.

Your settings are 0.1 lot total: 0.05 at TP1, 0.03 at TP2, 0.02 at TP3; no open portion. The signal's risk percentage is ignored for sizing, and the signal SL is retained.

If the parent group cannot be found, stop and list your accessible groups:

```powershell
.\.venv\Scripts\python.exe personal_trade_bot.py --list-chats
```

Find the intended parent group's numeric ID and set `$env:SOURCE_CHAT_IDS` to that ID. This explicit ID takes precedence over the parent-group name. Keep `$env:SOURCE_TOPIC_ID = "5"` for the selected topic. Do not use the topic name as a parent-group lookup. Do not set the topic to `all` for this configuration, as that would include other topics.

After closing PowerShell, repeat the API-credential and source-variable commands before starting again. The saved session normally avoids another phone/code login. The app does not read `.env` files automatically.

## MT5 is a separate connection

Telegram authorization does not enable trading. The MT5 execution connection still needs the intended account number, an installed/logged-in Windows terminal, a hedging account for the three TP positions, and the broker's exact gold symbol. See README.md for execution instructions and demo verification. This Windows setup guide stays in paper mode.

For quote-based paper testing after MT5 is configured, use `personal_trade_bot.py --watch-mt5`. It waits for the entry zone without sending orders. Execution mode uses the same waiting logic with actual orders enabled. A wait expires after 15 minutes by default (`ENTRY_WAIT_SECONDS=900`), a newer setup replaces an older waiting one, and disconnect cancels outstanding local waits. A stop cannot undo an order already submitted or filled. MT5 pending limit orders and automatic position-close/SL changes are not implemented.

To get help, share the success message or a redacted error. Do not send codes, passwords, API credentials, or session files.
