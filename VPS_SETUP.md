# XAU Trader Bot — VPS Setup Guide

Complete, step-by-step setup on a Windows VPS. Follow it top to bottom.

---

## Step 0 — What the VPS needs

| Requirement                | Notes                                                                                                                                   |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Windows** VPS            | Windows Server 2016+ or Windows 10/11. The `MetaTrader5` Python package only runs on Windows.                                           |
| **MetaTrader 5**           | Installed, **running**, and **logged in** to your account (demo or real). The **Algo Trading** button in the toolbar must be **green**. |
| **XAUUSD in Market Watch** | Right-click Market Watch → Symbols → find gold → **Show**.                                                                              |
| **Python 3.10+**           | During install, tick **"Add python.exe to PATH"**.                                                                                      |
| **Stable internet**        | The bot needs Telegram + the broker connection 24/7.                                                                                    |

Stop the VPS from sleeping (run CMD **as Administrator**):

```cmd
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 0
```

> MT5 must stay open and logged in. The bot re-checks the MT5 connection every 60 seconds and reconnects on its own, but it cannot work if the terminal is closed.

---

## Step 1 — Get the code

```cmd
cd C:\
git clone https://github.com/Erfangit23/rtyheirr9.git xau-trader-bot
cd xau-trader-bot
```

No Git installed? Open the GitHub page → **Code → Download ZIP** → extract to `C:\xau-trader-bot`.

---

## Step 2 — Install the dependencies

```cmd
install.bat
```

This creates a `venv` folder and installs `MetaTrader5`, `telethon` and `openai`.  
Verify it worked:

```cmd
venv\Scripts\python -c "import MetaTrader5, telethon; print('deps OK')"
```



---

## Step 3 — Create and fill `config.json`

`config.json` is **not** in the repository (it holds your credentials). Create it from the annotated template:

```cmd
copy config.template.json config.json
notepad config.json
```

### 3.1 Where each credential comes from

| Value                               | Where to get it                                                          |
| ----------------------------------- | ------------------------------------------------------------------------ |
| `api_id`, `api_hash`                | <https://my.telegram.org/apps> (log in with your phone, create an app)   |
| `phone`                             | Your own phone number, international format: `+98912...`                 |
| `bot_token`                         | Telegram → **@BotFather** → `/newbot` → copy the token                   |
| `authorized_user_ids`               | Telegram → **@userinfobot** → send `/start` → it replies your numeric ID |
| MT5 `login` / `password` / `server` | The MT5 login window. Copy `server` **exactly** as written.              |

### 3.2 Complete example (11 channels pre-filled)

```json
{
  "telegram": {
    "api_id": 1234567,
    "api_hash": "your_api_hash_here",
    "phone": "+989123456789",
    "session_name": "trader_session"
  },
  "report_bot": {
    "bot_token": "123456:ABC-DEF_your_bot_token",
    "authorized_user_ids": [123456789]
  },
  "mt5": {
    "login": 12345678,
    "password": "your_mt5_password",
    "server": "YourBroker-Server",
    "terminal_path": "",
    "symbol": "XAUUSD"
  },
  "ai": {
    "api_key": ""
  },
  "channels": [
    { "id": "@gold_alicxzos110",   "format": "format1" },
    { "id": "@Xsd_Gold_SignaIs1",  "format": "format2" },
    { "id": "@bttesteamin",        "format": "format8" },
    { "id": "@Gulljanali17",       "format": "format3" },
    { "id": "@GoLDVipSigbal",      "format": "format3" },
    { "id": "@BrianTradingForex",  "format": "format4" },
    { "id": "@forexkhan",          "format": "format5" },
    { "id": "@khanbouse",          "format": "format5" },
    { "id": "@Signal_Atlas",       "format": "format6" },
    { "id": "@GoldVisionofficial", "format": "format7" },
    { "id": "@Eliz_fxac_ademy1",   "format": "format8" }
  ],
  "trading": {
    "lot_size": 0.01,
    "default_tp_index": 2,
    "max_sl_pips": 150,
    "max_daily_sl_pips": 500,
    "max_open_trades": 5,
    "bot_active": true,
    "settings_password": "CHANGE_THIS"
  },
  "validation": {
    "mode": "dry_run",
    "ema200": { "enabled": true, "timeframe": "H1", "buffer_atr_mult": 0.3,
                "ext_guard": false, "ext_guard_atr_mult": 2.5 },
    "rsi":    { "enabled": true, "timeframe": "M15", "period": 14,
                "buy_max": 75, "sell_min": 25 },
    "atr_sl": { "enabled": true, "timeframe": "M15", "period": 14,
                "min_sl_atr_mult": 0.5 }
  }
}
```

### 3.3 What every field means

**`telegram`** — your personal Telegram account (not the bot). Used to read the signal channels. On the first run you enter a login code once; afterwards the `trader_session.session` file remembers it.

**`report_bot`** — the bot that sends you reports and receives your commands.  
`authorized_user_ids` is a security list: **only these IDs can control the bot.** Put only your own.

**`mt5`**

- `login` / `password` / `server` — your MT5 account. For a real account use the real values; the account must also be logged in inside the MT5 terminal.
- `terminal_path` — leave `""` if MT5 is installed in the default location.
- `symbol` — **critical**: the exact gold symbol name your broker uses (`XAUUSD`, `XAUUSD.pro`, `GOLD`, …). If it is wrong the bot cannot see any position and every order fails.

**`ai`** — optional. Put your NVIDIA API key here to enable AI signal parsing + Persian commentary. Leave empty and the bot simply uses the regex parsers. This file is gitignored, so the key stays on your VPS only.

**`channels`** — the signal channels to monitor. Your Telegram account must be a member of each one.

- `id` — the channel username **with** `@`.
- `format` — which parser to use (see the table in 3.4). Use `"auto"` if unsure.
- Optional per channel: `"active": false` (temporarily disable) and `"filters": false` (skip the quality filters for that channel only).

**`trading`**

| Field               | Meaning                                                                                               |
| ------------------- | ----------------------------------------------------------------------------------------------------- |
| `lot_size`          | Base lot per trade (0.01 → about $1 per 10 pips on gold)                                              |
| `default_tp_index`  | Which TP to use for channels without a special rule                                                   |
| `max_sl_pips`       | Reject a signal whose stop distance is larger than this                                               |
| `max_daily_sl_pips` | Daily loss cap; once today's realised loss reaches it, new trades stop until the next day (`0` = off) |
| `max_open_trades`   | Max simultaneous gold trades, positions + pending (`0` = unlimited)                                   |
| `bot_active`        | `false` = sleep, place nothing                                                                        |
| `settings_password` | Password for `/change` in Telegram — **change it**                                                    |

**`validation`** — the signal quality filters (EMA200 trend, RSI exhaustion, ATR stop floor).  
`"mode"` is `off`, `dry_run` (report only, still trade) or `on` (block). **Start with `dry_run`** and check `/backtest` per channel before switching to `on`.

### 3.4 Channel format reference

| Format    | Message style                                                                     |
| --------- | --------------------------------------------------------------------------------- |
| `format1` | `📊XAUUSD SELL NOW ( 4167 )` + `📊TARGET 1 ( 4163 )` + `🚫 STOP LOSS ( 4177 )`    |
| `format2` | `XAUUSD SELL NOW 4171:::4180` + `✔️ Tp1 🔽 4166` + `❌ SL 4186`                    |
| `format3` | `XAUUSD Sell 4064` + `TP 4059` + `SL 4074` (plain, no emoji)                      |
| `format4` | `🧑‍💻 XAU/USD Buy 4063 - 4060` + `📌 Stoploss : 4057` + `Take Profit : 4068`     |
| `format5` | `XAUUSD ( اسکلپ )` + `Buy now : 4090 - 4086` + `Tp1 : 4096` + `Sl : 4080`         |
| `format6` | `⚜️ XAUUSD - BUY NOW` + `🛒 Entry : 4081` + `🎯 Targets :` + `🔺 Stoploss : 4076` |
| `format7` | `XAUUSD \| SELL 📈` + `Entry : 4043` + `✔️TP1: 4039` + `❌SL : 4052`               |
| `format8` | `GOLD Buy Limit 4252-4253` + `TP 4259` + `SL 4246`                                |

> Per-channel rules (TP index, entry/SL adjustments, step-up SL, dual entry, cooldowns) are built into the code by channel name — you do not configure them.

---

## Step 4 — First run (Telegram login)

```cmd
start.bat
```

The first time, Telegram sends you a login code — type it in the console window. A `trader_session.session` file is created; from then on no code is needed. **Back this file up and never share it.**

---

## Step 5 — Verify it works

In Telegram, **send `/start` to your own report bot first** (a bot cannot message you until you start it). Then:

| Command     | Expected                                                   |
| ----------- | ---------------------------------------------------------- |
| `/status`   | `Bot: ☀️ ACTIVE`, `MT5: ✅ Connected`, balance/equity       |
| `/settings` | lot 0.01, TP2, max SL 150, daily 500, Max Open Trades: 5   |
| `/channels` | all 11 channels with ✅ and per-channel stats               |
| `/trades`   | open positions and pending orders                          |
| `/report`   | today's deals + `Daily loss: X/500 pips`                   |
| `/backtest` | pick a channel → historical winrate + 🧪 filter simulation |

And check the log file:

```cmd
type logs\trader_*.log
```

You should see `All systems ready` and `Monitoring 11 channels: [...]`.

---

### If Telegram login or channel access misbehaves

Run the built-in read-only checker (it reads `config.json`, so no credentials go into any script):

```cmd
venv\Scripts\python check_telegram.py
```

It tells you:
- whether your `api_id` / `api_hash` pair is still valid (or revoked/flagged),
- which Telegram account the saved session belongs to,
- **which of your channels that account can actually read** — the most common cause of "no signals" is that the logged-in account is not a member of a channel.

It never sends a login code and never changes anything.

## Step 6 — Run it 24/7

`start.bat` already restarts the bot 10 seconds after any crash. To also start it automatically after a VPS reboot, create a scheduled task:

1. `Win+R` → `taskschd.msc` → **Create Task**
2. **General** tab: name `XAU Trader Bot` → tick **Run whether user is logged on or not** and **Run with highest privileges**
3. **Triggers** tab → New → **At startup**
4. **Actions** tab → New → Program/script: `C:\xau-trader-bot\start.bat` — **Start in**: `C:\xau-trader-bot`
5. **Settings** tab → tick *If the task fails, restart every 1 minute*; set *If the task is already running: Do not start a new instance*; **untick** *Stop the task if it runs longer than…*
6. Save (enter your Windows password if asked)

Also make MT5 start automatically: `Win+R` → `shell:startup` → put a shortcut to `terminal64.exe` there.

---

## If the bot stops by itself (the VPS restarted / apps closed)

This is almost always the VPS **rebooting** (Windows Update is the usual
culprit) or the remote session being **logged off**. A bot started by hand dies
with it. Four things prevent that:

### 1. Make the VPS log in automatically after a reboot

MetaTrader 5 is a desktop app: it needs a logged-in session. Without auto-login
the terminal (and therefore the bot) cannot start after a restart.

```cmd
netplwiz
```
Untick *"Users must enter a user name and password"*, apply, and enter the
Windows password once. (On Windows Server: `control userpasswords2`.)

### 2. Register the bot as a scheduled task (not a hand-started window)

Task Scheduler → **Create Task**:

| Tab | Setting |
|---|---|
| General | name `XAU Trader Bot`; **Run only when user is logged on** (with auto-login this is always true, and it keeps the bot in the same session as MT5); **Run with highest privileges** |
| Triggers | **At log on** (and *At startup* as a backup) |
| Actions | Program: `C:\xau-trader-bot\start.bat` — Start in: `C:\xau-trader-bot` |
| Settings | *If the task is already running: Do not start a new instance*; untick *Stop the task if it runs longer than…* |

Add a second task for MetaTrader itself (same trigger, Program = `terminal64.exe`),
or put a shortcut to `terminal64.exe` in `shell:startup`.

### 3. Add the watchdog (restarts anything that dies later)

Create a task running **every 5 minutes**:

| Tab | Setting |
|---|---|
| General | same user as the bot, **Run only when user is logged on**, highest privileges |
| Triggers | Daily → **Repeat task every 5 minutes** for a duration of *Indefinitely* |
| Actions | Program: `C:\xau-trader-bot\watchdog.bat` — Start in: `C:\xau-trader-bot` |

`watchdog.bat` checks every 5 minutes that **terminal64.exe** and the **bot's
python.exe** are running, starts whichever is missing, and logs only when it had
to act (to `logs\watchdog.log`).

### 4. Stop Windows Update from rebooting the VPS

This is the #1 cause of "the bot died overnight" — verified on a real VPS by the
System event log: `MoUsoCoreWorker.exe` + `TrustedInstaller.exe` initiating a
restart at 00:41 with reason *Operating System: Upgrade (Planned)*.

First check which Windows you have:

```powershell
(Get-CimInstance Win32_OperatingSystem).Caption
```

**A. Windows Server → `sconfig`**
```cmd
sconfig
```
→ option **5** (Windows Update settings) → option **2** (Download only) → `15` to exit.

**B. Windows 10/11, or a Server image without `sconfig`** — run these in an
**elevated** prompt (works on both editions):

```powershell
REM never auto-reboot while a user is logged on
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v AUOptions /t REG_DWORD /d 2 /f

REM disable the task that actually performs the update reboot
schtasks /Change /TN "\Microsoft\Windows\UpdateOrchestrator\Reboot" /Disable
```

Verify:
```powershell
schtasks /Query /TN "\Microsoft\Windows\UpdateOrchestrator\Reboot" /FO LIST | findstr /I "TaskName Status"
```

GUI equivalent (Windows Pro/Server): `gpedit.msc` → Computer Configuration →
Administrative Templates → Windows Components → Windows Update → **"No auto-restart
with logged on users for scheduled automatic updates installations"** → Enabled.

**C. Nuclear option (no automatic updates at all)** — only if you are happy to
patch the server yourself on a schedule:

```powershell
sc.exe config wuauserv start= demand
sc.exe config UsoSvc start= disabled
```

Whichever you choose, remember the auto-login + log-on tasks + watchdog (items
1–3) are still what protects you from *every other* reboot cause — provider
maintenance, power loss, a crash.

Also make sure the power plan never sleeps (Step 0).

### How to find out what actually happened

```cmd
REM when did the machine last boot?
systeminfo | findstr /C:"System Boot Time"

REM what restarted it: 1074 = a process asked for a restart,
REM 41 = power loss, 6008 = unexpected shutdown
wevtutil qe System /q:"*[System[(EventID=1074 or EventID=41 or EventID=6008)]]" /c:10 /rd:true /f:text

REM what was the bot doing when it died?
type logs\trader_*.log

REM did the watchdog have to intervene?
type logs\watchdog.log
```

If the bot log simply stops mid-sentence with no error, the process was killed
(reboot/logoff) rather than crashed — items 1–4 above fix that.

### Make the bot able to bring MT5 back by itself

Set `mt5.terminal_path` in `config.json` to the full path of `terminal64.exe`
(e.g. `C:\Program Files\MetaTrader 5\terminal64.exe`). With it, the bot's
60-second keepalive can **launch** MetaTrader if it was closed, instead of only
reporting that it cannot connect. Leave it empty only if MT5 is always running
and you never want the bot to start it.

## Step 7 — Pre-live checklist

- [ ] Run on a **demo account for 2–3 days** first; watch `/report` and `/trades`
- [ ] `settings_password` changed from the default
- [ ] `authorized_user_ids` contains only you
- [ ] `mt5.symbol` matches the broker exactly
- [ ] MT5 terminal open, logged in, Algo Trading green
- [ ] Caps as intended: lot `0.01`, `max_sl_pips` 150, `max_daily_sl_pips` 500, `max_open_trades` 5
- [ ] Filters still `dry_run` until `/backtest` shows `✅ Filters HELP` for the channels you want
- [ ] Backups taken of `config.json`, `trader_session.session` and the `data\` folder

---

## Step 8 — Updating the bot

```cmd
cd C:\xau-trader-bot
git pull
```

Then restart it (close the bot window and run `start.bat`, or restart the scheduled task).  
`config.json` is never overwritten by an update, because it is not part of the repository.

---

## Files the bot creates (back these up)

| Path                     | What it is                    |
| ------------------------ | ----------------------------- |
| `config.json`            | Your settings and credentials |
| `trader_session.session` | Telegram login session        |
| `data\trades.json`       | Trade history                 |


| `data\linked_orders.json` | Breakeven links (Brian dual entry) |
| `data\sl_cooldown.json` | Per-channel SL cooldowns |
| `logs\trader_YYYYMMDD.log` | Daily log |

---

## Making the filters stricter (the right way)

A filter only helps if it blocks trades that would have LOST. On a channel that
wins 2 out of 3 trades, simply blocking more signals makes things worse — so
tune with numbers, not with guesses.

1. Run `/backtest` and pick a channel.
2. Read the **📐 Threshold scan** section of the report. It replays the SAME
   trades against several candidate thresholds and shows, for each one:

   ```
   📐 Threshold scan (same trades, different thresholds):
     EMA200 buffer 0.3x: blocks 14 (7W/5L) net +375720→+375570 (-150) ⚠️ ←current
     EMA200 buffer 0.0x: blocks 31 (5W/26L) net +375720→+381004 (+5284) ✅
     🏆 Best combo (EMA200 buffer 0.0x + RSI 70/30): blocks 61 (11W/50L),
        net +375720→+384000 (+8280) ✅ HELPS
   ```

   `←current` marks what you use today. ✅ = would have helped, ⚠️ = would have hurt.
3. Fastest option — apply a preset, then verify with the scan:
   - `/filterpreset strict`   → EMA buffer 0x, RSI 60/40, ATR floor 0.8x
   - `/filterpreset balanced` → EMA buffer 0.3x, RSI 70/30, ATR floor 0.5x
   - `/filterpreset off`      → back to the original lenient values
   Every scan also prints a **Strict preset** row, so you can see what the
   strict combination would have done *before* applying it.
4. Or tune one filter at a time — no file editing needed:
   - `/femabuf 0`   → EMA200 neutral zone in ATR units (`0` = strictest)
   - `/rsith 70/30` → reject BUY at/above 70, SELL at/below 30
   - `/atrfloor 0.8` → reject stops tighter than 0.8 x ATR(M15)
5. Re-run `/backtest` to confirm the improvement, then `/filtermode on`.
6. Per channel: if a channel does better with filters off, use `/fema off @channel`
   (or `"filters": false` in its config entry).

Notes: the scan ignores a threshold that would block more than half of the
trades (that is a shutdown, not a filter) and stays silent on samples smaller
than 10 closed trades. If a filter shows **⚠️ NO DATA**, the backtest could not
read enough history for it at signal time — that filter was inactive, so its
"0 blocked" means nothing.

## Troubleshooting

| Message / symptom | Cause | Fix |
|---|---|---|
| `Cannot connect to MetaTrader 5` | MT5 closed, not logged in, or wrong `terminal_path` | Open MT5, log in, enable Algo Trading |
| `Symbol XAUUSD not found in MT5` | Wrong symbol name | Set `mt5.symbol` to the broker's exact name |
| `No parser matched` | Wrong `format` for that channel | Check the raw message in the log, fix `format` or use `"auto"` |
| `🚫 Order REJECTED - SL too large` | Signal's stop is wider than `max_sl_pips` | Normal — the signal was too risky |
| `⏭️ Order SKIPPED - market already past entry` | Price moved before the order was placed | Normal |
| `🛑 max open trades reached` | 5 trades already open | Normal — wait for one to close |
| `🛑 daily loss cap reached` | Today's loss hit `max_daily_sl_pips` | Normal — resumes next day |
| `♻️ Duplicate signal ignored` | Channel re-posted a call still pending | Normal — prevents double exposure |
| No Telegram reports | You never sent `/start` to the bot, or wrong token / user ID | Send `/start` to the bot; check `bot_token` and `authorized_user_ids` |
| Order stays pending, never fills | It is a **limit** order waiting for the price | Normal — after a 10-minute grace it cancels itself if price is at/beyond TP2 without filling |
| "Dual entry placed" then instantly "BOTH orders CANCELLED" | Fixed: the cancel rule had no grace period and fired on the first cycle when price was already past TP2 | `git pull` — orders now get 10 minutes to fill first |
| Bot restarts in a loop every 10s | Bad `config.json` (missing credentials) | Read the console error, fix `config.json` |
| `Backtest failed ... could not convert string to float: '.'` | A channel posted/edited a message with a missing price | Fixed — `git pull` and restart |
| Telegram login fails, or some channels never produce signals | Wrong/revoked api credentials, or the logged-in account is not a member | Run `venv\Scripts\python check_telegram.py` (see below) |

---

## Telegram command reference

**No password needed:** `/start` `/status` `/settings` `/channels` `/trades` `/report` `/backtest` `/filters` `/filtermode off|dry|on` `/fema /frsi /fatr on|off [@channel]` `/femabuf <0-5>` `/rsith <buy>/<sell>` `/atrfloor <0.1-3>` `/filterpreset strict|balanced|off` `/cooldown <minutes>` `/makeaion` `/makeaioff`

**After `/change` + password:** `lot <size>` `tp <index>` `maxsl <pips>` `dailysl <pips>` `maxtrades <n>` `sleep` `wake` `chan on|off <@channel>` `done`
