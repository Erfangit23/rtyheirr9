"""
Test: filter threshold tuning from Telegram (/femabuf, /rsith, /atrfloor).

Verifies:
  - each command sets the matching validation value and persists to config.json
  - bad input (missing / non-numeric / out of range / wrong order) is rejected
    and leaves the setting untouched
  - /filters shows the current thresholds and the tuning hint

Pure mocks — no MT5, no Telegram, no network.
Run:  python test_filter_tuning.py
"""

import sys
import os
import json
import asyncio
import tempfile

sys.modules.setdefault("MetaTrader5", type(sys)("MetaTrader5"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                  # noqa: E402
from bot_commands import CommandHandler        # noqa: E402


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@TestCh", "format": "auto"}],
        "trading": {"lot_size": 0.01, "bot_active": True},
        "validation": {
            "mode": "dry_run",
            "ema200": {"enabled": True, "timeframe": "H1", "buffer_atr_mult": 0.3},
            "rsi": {"enabled": True, "timeframe": "M15", "period": 14,
                    "buy_max": 75, "sell_min": 25},
            "atr_sl": {"enabled": True, "timeframe": "M15", "period": 14,
                       "min_sl_atr_mult": 0.5},
        },
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path, Settings(path)


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xautune_")
    path, s = make_settings(tmpdir)
    h = CommandHandler(settings=s, mt5=None)
    run = lambda t: asyncio.run(h.handle(t, 111))

    def ema_buf():
        return s.filter_config_for_channel(None, "ema200")["buffer_atr_mult"]

    def rsi_thr():
        c = s.filter_config_for_channel(None, "rsi")
        return (c["buy_max"], c["sell_min"])

    def atr_floor():
        return s.filter_config_for_channel(None, "atr_sl")["min_sl_atr_mult"]

    print("-- defaults --")
    _check(failures, f"ema buffer 0.3 (got {ema_buf()})", ema_buf() == 0.3)
    _check(failures, f"rsi 75/25 (got {rsi_thr()})", rsi_thr() == (75, 25))
    _check(failures, f"atr floor 0.5 (got {atr_floor()})", atr_floor() == 0.5)

    print("-- /femabuf --")
    _check(failures, "strictest 0 accepted", "0x ATR" in run("/femabuf 0") and ema_buf() == 0.0)
    _check(failures, "0.5 accepted", "0.5x ATR" in run("/femabuf 0.5") and ema_buf() == 0.5)
    _check(failures, "no argument -> usage", "Usage" in run("/femabuf"))
    _check(failures, "non-numeric rejected", "❌" in run("/femabuf abc") and ema_buf() == 0.5)
    _check(failures, "out of range rejected", "❌" in run("/femabuf 9") and ema_buf() == 0.5)

    print("-- /rsith --")
    _check(failures, "70/30 accepted", "70" in run("/rsith 70/30") and rsi_thr() == (70.0, 30.0))
    _check(failures, "space form accepted", "65" in run("/rsith 65 35") and rsi_thr() == (65.0, 35.0))
    _check(failures, "no argument -> usage", "Usage" in run("/rsith"))
    _check(failures, "reversed order rejected", "❌" in run("/rsith 30/70") and rsi_thr() == (65.0, 35.0))
    _check(failures, "non-numeric rejected", "❌" in run("/rsith abc") and rsi_thr() == (65.0, 35.0))
    _check(failures, "out of range rejected", "❌" in run("/rsith 120/10") and rsi_thr() == (65.0, 35.0))

    print("-- /atrfloor --")
    _check(failures, "0.8 accepted", "0.8x" in run("/atrfloor 0.8") and atr_floor() == 0.8)
    _check(failures, "no argument -> usage", "Usage" in run("/atrfloor"))
    _check(failures, "too small rejected", "❌" in run("/atrfloor 0.01") and atr_floor() == 0.8)
    _check(failures, "too large rejected", "❌" in run("/atrfloor 5") and atr_floor() == 0.8)

    print("-- persistence --")
    s2 = Settings(path)
    _check(failures, f"ema buffer persisted (got {s2.filter_config_for_channel(None,'ema200')['buffer_atr_mult']})",
           s2.filter_config_for_channel(None, "ema200")["buffer_atr_mult"] == 0.5)
    _check(failures, "rsi persisted", tuple(s2.filter_config_for_channel(None, "rsi")[k]
                                            for k in ("buy_max", "sell_min")) == (65.0, 35.0))
    _check(failures, "atr floor persisted", s2.filter_config_for_channel(None, "atr_sl")["min_sl_atr_mult"] == 0.8)

    print("-- /filters shows the values + hint --")
    out = run("/filters")
    _check(failures, "shows EMA200 buffer", "neutral buffer 0.5xATR" in out)
    _check(failures, "shows RSI thresholds", "Buy >= 65" in out)
    _check(failures, "shows ATR floor", "min SL 0.8x" in out)
    _check(failures, "mentions the tuning commands", "/femabuf" in out and "/rsith" in out and "/atrfloor" in out)
    _check(failures, "mentions the threshold scan", "threshold scan" in out)

    print("-- /filterpreset --")
    _check(failures, "usage without a name", "Usage" in run("/filterpreset"))
    _check(failures, "unknown preset rejected", "Usage" in run("/filterpreset bogus"))
    _check(failures, "values unchanged after bad input",
           ema_buf() == 0.5 and rsi_thr() == (65.0, 35.0) and atr_floor() == 0.8)

    out = run("/filterpreset strict")
    _check(failures, f"strict: ema 0x (got {ema_buf()})", ema_buf() == 0.0)
    _check(failures, f"strict: rsi 60/40 (got {rsi_thr()})", rsi_thr() == (60.0, 40.0))
    _check(failures, f"strict: atr 0.8x (got {atr_floor()})", atr_floor() == 0.8)
    _check(failures, "strict reply mentions dry-run", "dry-run" in out)
    _check(failures, "strict reply points at /backtest", "/backtest" in out)

    run("/filterpreset balanced")
    _check(failures, f"balanced: ema 0.3x (got {ema_buf()})", ema_buf() == 0.3)
    _check(failures, f"balanced: rsi 70/30 (got {rsi_thr()})", rsi_thr() == (70.0, 30.0))
    _check(failures, f"balanced: atr 0.5x (got {atr_floor()})", atr_floor() == 0.5)

    run("/filterpreset off")
    _check(failures, f"off: original lenient values (got {rsi_thr()})", rsi_thr() == (75.0, 25.0))

    _check(failures, "/filters mentions the preset command",
           "/filterpreset" in run("/filters"))

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== filter threshold tuning test ===")
    main()
