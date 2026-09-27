# XAU Trader Bot

Automated Telegram-signal-to-MetaTrader5 trading bot for XAUUSD.

## What It Does

1. Monitors Telegram channels for trading signals
2. Parses entry, stop loss, and take profit levels
3. Places limit orders on MetaTrader 5
4. Reports all trade activity via a Telegram bot
5. Lets you control settings via the Telegram bot (password protected)

## Quick Start

### Prerequisites
- Windows VPS with MetaTrader 5 installed and logged in
- Python 3.10+ installed
- Telegram account that is a member of the signal channels
- A Telegram bot token (from @BotFather)

### Installation

1. Run `install.bat` — this creates a virtual environment and installs dependencies.

2. Edit `config.json` with your details:

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
    "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "symbol": "XAUUSD"
  },
  "channels": [
    { "id": "@gold_alicxzos110", "format": "format1" },
    { "id": "@Xsd_Gold_SignaIs1", "format": "format2" }
  ],
  "trading": {
    "lot_size": 0.01,
    "default_tp_index": 2,
    "max_sl_pips": 150,
    "max_daily_sl_pips": 500,
    "bot_active": true,
    "settings_password": "Amin123"
  }
}
```

3. Get your Telegram `api_id` and `api_hash`:
   - Go to https://my.telegram.org/apps
   - Log in with your phone number
   - Create an application
   - Copy the api_id and api_hash

4. Get your Telegram user ID:
   - Send `/start` to @userinfobot on Telegram
   - It will reply with your numeric user ID
   - Put this in `authorized_user_ids`

5. Create the report bot:
   - Open @BotFather on Telegram
   - Send `/newbot`
   - Follow the prompts
   - Copy the bot token into `config.json`

6. Make sure MetaTrader 5 is running and logged into your account.

7. Run `start.bat` to start the bot.

### First Run

On the first run, the bot will ask for a Telegram login code. Enter the code sent to your Telegram app. This only happens once — the session is saved for future runs.

## Bot Commands (via Telegram report bot)

| Command | Description |
|---------|-------------|
| `/start` | Show available commands |
| `/status` | Bot & MT5 connection status |
| `/settings` | View current trading settings |
| `/trades` | Show open positions and pending orders |
| `/report` | Today's trade summary |
| `/change` | Start settings change (requires password) |

After `/change`, enter password `Amin123`, then use:

| Command | Description |
|---------|-------------|
| `lot 0.02` | Set lot size |
| `tp 3` | Set default TP index (1-10) |
| `maxsl 200` | Set max SL per trade (pips) |
| `dailysl 600` | Set max daily SL (pips) |
| `sleep` | Pause trading (no new orders) |
| `wake` | Resume trading |
| `done` | Exit settings mode |

## How Trading Works

1. Signal comes from a monitored channel
2. Bot parses entry, SL, and all TP levels
3. Bot places a **limit order** at the entry price
4. TP is set to the configured TP index (default: TP2)
5. SL is set exactly as the channel specifies
6. If SL distance > 150 pips (configurable), the order is **NOT placed**
7. Only XAUUSD signals are processed
8. The closest-to-fill entry is used (Entry 1, not Entry 2)

## Files

- `main.py` — Entry point, orchestrates everything
- `config.json` — All credentials and settings
- `signal_parser.py` — Parses two signal formats
- `mt5_connector.py` — MetaTrader 5 connection and order execution
- `telegram_client.py` — Telegram monitoring and report bot
- `trade_manager.py` — Trade lifecycle management
- `bot_commands.py` — Telegram bot command handler
- `settings.py` — Thread-safe settings manager
- `start.bat` — Auto-restart wrapper for 24/7 operation
- `install.bat` — One-time setup script

## Safety Features

- SL distance check (default max: 150 pips)
- Max concurrent open trades cap (default: 5, `max_open_trades` in config; 0 = unlimited)
- Daily SL limit tracking
- Bot sleep/wake toggle
- Password-protected settings changes
- Only processes XAUUSD signals
- Only responds to authorized Telegram users
- All trades logged with full audit trail

## Logs

Logs are saved to `logs/trader_YYYYMMDD.log`.

## 24/7 Operation

`start.bat` automatically restarts the bot if it crashes, with a 10-second delay.
