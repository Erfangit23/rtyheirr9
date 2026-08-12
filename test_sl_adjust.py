"""
Test: @Gulljanali17 / @bttesteamin SL tightening.

Verifies that for these two channels the SL is moved 10 pips closer to entry
(reducing risk), while the ENTRY and all TAKE-PROFIT levels are left untouched.

Pure mocks — no MT5 connection, no Telegram, no network.
Run:  python test_sl_adjust.py
"""

import sys
import os
import asyncio
import tempfile

# Inject a harmless fake MetaTrader5 so `import mt5_connector` succeeds.
# (We never call the real connector here — we use FakeMT5Connector below.)
sys.modules.setdefault("MetaTrader5", type(sys)("MetaTrader5"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                  # noqa: E402
from signal_parser import Signal               # noqa: E402
from trade_manager import TradeManager         # noqa: E402


class FakeMT5Connector:
    """Captures the signal passed to place_limit_order so we can inspect it."""

    def __init__(self):
        self.connected = True
        self.captured = None  # the signal handed to place_limit_order

    def ensure_connected(self):
        return True

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4000.0, 4000.1)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.captured = signal
        return 99999  # fake successful ticket


def make_settings(tmpdir):
    import json
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@Gulljanali17", "format": "format3"}],
        "trading": {
            "lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
            "max_daily_sl_pips": 500, "bot_active": True,
            "settings_password": "x",
        },
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def _check(failures, label, cond):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        failures.append(label)


def run_scenarios():
    tmpdir = tempfile.mkdtemp(prefix="xausl_")
    prev_cwd = os.getcwd()
    os.chdir(tmpdir)
    failures = []

    try:
        settings = make_settings(tmpdir)
        mt5 = FakeMT5Connector()
        tm = TradeManager(settings=settings, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None

        def run(direction, entry, sl, tps, channel):
            mt5.captured = None
            sig = Signal(symbol="XAUUSD", direction=direction, entry=entry,
                         stop_loss=sl, take_profits=list(tps), source_channel=channel)
            asyncio.run(tm.process_signal(sig))
            return mt5.captured

        # --- @Gulljanali17, SELL: SL above entry, should drop 10 pips (1.0) ---
        c = run("SELL", entry=4010.0, sl=4020.0, tps=[4000.0, 3990.0],
                channel="@Gulljanali17")
        _check(failures, "@Gulljanali17 SELL: SL 4020 -> 4019",
               c is not None and c.stop_loss == 4019.0)
        _check(failures, "@Gulljanali17 SELL: entry unchanged 4010",
               c is not None and c.entry == 4010.0)
        _check(failures, "@Gulljanali17 SELL: TPs unchanged",
               c is not None and c.take_profits == [4000.0, 3990.0])

        # --- @Gulljanali17, BUY: SL below entry, should rise 10 pips (1.0) ---
        c = run("BUY", entry=4000.0, sl=3990.0, tps=[4010.0, 4020.0],
                channel="@Gulljanali17")
        _check(failures, "@Gulljanali17 BUY: SL 3990 -> 3991",
               c is not None and c.stop_loss == 3991.0)
        _check(failures, "@Gulljanali17 BUY: entry unchanged 4000",
               c is not None and c.entry == 4000.0)
        _check(failures, "@Gulljanali17 BUY: TPs unchanged",
               c is not None and c.take_profits == [4010.0, 4020.0])

        # --- @bttesteamin gets the same treatment ---
        c = run("SELL", entry=4010.0, sl=4020.0, tps=[4000.0, 3990.0],
                channel="@bttesteamin")
        _check(failures, "@bttesteamin SELL: SL 4020 -> 4019",
               c is not None and c.stop_loss == 4019.0)
        _check(failures, "@bttesteamin SELL: entry unchanged 4010",
               c is not None and c.entry == 4010.0)

        # --- A channel NOT in the list is left alone ---
        c = run("SELL", entry=4010.0, sl=4020.0, tps=[4000.0, 3990.0],
                channel="@forexkhan")
        _check(failures, "@forexkhan SELL: SL unchanged 4020 (not adjusted)",
               c is not None and c.stop_loss == 4020.0)

        return failures
    finally:
        os.chdir(prev_cwd)


if __name__ == "__main__":
    print("=== SL tightening test (@Gulljanali17 / @bttesteamin) ===")
    failures = run_scenarios()
    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
