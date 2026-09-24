# XAU Trader Bot — Long-term Project Notes

## What this project is
Telegram-signal-to-MT5 automated XAUUSD trading bot. Telethon user session monitors ~10 gold signal channels → parses signals (8 regex formats + optional NVIDIA DeepSeek AI parser) → places limit orders on MT5 (magic 779900) → reports to a Telegram bot → 24/7 on a Windows VPS via start.bat.

## Key architecture
- Per-channel rules in trade_manager.process_signal: TP index overrides (gold_alicxzos110 TP4, GoldVisionofficial TP3, khan* TP1, Signal_Atlas TP2, Eliz TP1, Brian dual-entry), Gulljanali17/bttesteamin entry+10p & SL-tighten-10p, SL cooldown 90min for Gulljanali17, 248 martingale (1,2,4,8...) per channel, step-up SL logic, linked-order breakeven for Brian dual entry.
- KHAN_CHANNELS aliases: @forexkhan @khanbours @khanbourse @khanbouse.
- Bot commands: /status /settings /channels /trades /report /backtest /filters /filtermode /fema /frsi /fatr /makeaion /248on /248off /248status + password-protected /change (password Amin123).
- Config: config.json gitignored; local copy has credentials stripped (dev copy, VPS has the real one).

## Conventions
- 1 pip gold = 0.1 price units; $0.10 per pip per 0.01 lot.
- Tests are mock-based, no MT5/Telegram/network needed: run each test_*.py directly.
- newconfig.json is a tracked spare config (also credential-stripped except MT5 demo creds).

## Known open bugs (as of 2026-09-24, see daily log)
Daily SL cap not enforced; AI mode double API call; modify ignores new_tp; NVIDIA API key hardcoded in tracked source files.
