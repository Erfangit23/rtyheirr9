"""
Test: @Gulljanali17 / @bttesteamin entry+SL adjustment.

Behavior under test:
  - Entry moves 10 pips closer to market (BUY 4400 -> 4401, SELL 4400 -> 4399).
  - SL tightens 10 pips toward the original SL's side.
  - On a symmetric 100-pip signal this yields $10 SL / $9 TP, i.e. the SL
    distance from the adjusted entry is 10.0 and the TP1 distance is 9.0.
  - TP levels themselves are not modified.
  - Other channels are left alone.

Pure mocks — no MT5 connection, no Telegram, no network.
Run:  python test_sl_adjust.py
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
from signal_parser import Signal               # noqa: E402
from trade_manager import TradeManager         # noqa: E402


class FakeMT5:
    def __init__(self):
        self.bid = 4404.0
        self.ask = 4405.0
        self.captured = None
        self.force_ticket = None
        self.calls = 0

    def ensure_connected(self):
        return True

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (self.bid, self.ask)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.calls += 1
        if self.force_ticket is not None:
            return self.force_ticket
        self.captured = signal
        return 70001


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@Gulljanali17", "format": "format3"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "bot_active": True,
                    "settings_password": "x"},
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
    prev = os.getcwd()
    os.chdir(tmpdir)
    failures = []

    try:
        settings = make_settings(tmpdir)
        mt5 = FakeMT5()
        tm = TradeManager(settings=settings, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None

        def run(direction, entry, sl, tps, channel, bid, ask):
            mt5.bid, mt5.ask = bid, ask
            mt5.captured = None
            sig = Signal(symbol="XAUUSD", direction=direction, entry=entry,
                         stop_loss=sl, take_profits=list(tps), source_channel=channel)
            asyncio.run(tm.process_signal(sig))
            return mt5.captured

        # --- @Gulljanali17 BUY, symmetric 100 pips ---
        c = run("BUY", entry=4400.0, sl=4390.0, tps=[4410.0, 4420.0],
                channel="@Gulljanali17", bid=4404.0, ask=4405.0)
        _check(failures, "BUY entry 4400 -> 4401 (+10)", c and c.entry == 4401.0)
        _check(failures, "BUY SL 4390 -> 4391 (+10)", c and c.stop_loss == 4391.0)
        _check(failures, "BUY TPs unchanged", c and c.take_profits == [4410.0, 4420.0])
        if c:
            sl_dist = c.entry - c.stop_loss          # 4401 - 4391 = 10.0
            tp_dist = c.take_profits[0] - c.entry    # 4410 - 4401 = 9.0
            _check(failures, f"BUY ratio -> SL {sl_dist}/TP {tp_dist} ($10/$9)",
                   abs(sl_dist - 10.0) < 1e-9 and abs(tp_dist - 9.0) < 1e-9)

        # --- @Gulljanali17 SELL, symmetric 100 pips ---
        c = run("SELL", entry=4400.0, sl=4410.0, tps=[4390.0, 4380.0],
                channel="@Gulljanali17", bid=4395.0, ask=4396.0)
        _check(failures, "SELL entry 4400 -> 4399 (-10)", c and c.entry == 4399.0)
        _check(failures, "SELL SL 4410 -> 4409 (-10)", c and c.stop_loss == 4409.0)
        _check(failures, "SELL TPs unchanged", c and c.take_profits == [4390.0, 4380.0])
        if c:
            sl_dist = c.stop_loss - c.entry          # 4409 - 4399 = 10.0
            tp_dist = c.entry - c.take_profits[0]    # 4399 - 4390 = 9.0
            _check(failures, f"SELL ratio -> SL {sl_dist}/TP {tp_dist} ($10/$9)",
                   abs(sl_dist - 10.0) < 1e-9 and abs(tp_dist - 9.0) < 1e-9)

        # --- @bttesteamin gets the same treatment ---
        c = run("SELL", entry=4400.0, sl=4410.0, tps=[4390.0, 4380.0],
                channel="@bttesteamin", bid=4395.0, ask=4396.0)
        _check(failures, "@bttesteamin SELL SL 4410 -> 4409", c and c.stop_loss == 4409.0)
        _check(failures, "@bttesteamin SELL entry 4400 -> 4399", c and c.entry == 4399.0)

        # --- other channel: untouched ---
        c = run("SELL", entry=4400.0, sl=4410.0, tps=[4390.0, 4380.0],
                channel="@forexkhan", bid=4395.0, ask=4396.0)
        _check(failures, "@forexkhan SELL entry unchanged 4400", c and c.entry == 4400.0)
        _check(failures, "@forexkhan SELL SL unchanged 4410", c and c.stop_loss == 4410.0)

        # --- SL-too-large (-1) is reported as REJECTED_SL, not "MT5 code 1" ---
        mt5.force_ticket = -1
        sig = Signal(symbol="XAUUSD", direction="SELL", entry=4400.0,
                     stop_loss=4540.0, take_profits=[4380.0], source_channel="@forexkhan")
        asyncio.run(tm.process_signal(sig))
        mt5.force_ticket = None
        rec = tm.trades[-1] if tm.trades else None
        _check(failures, f"-1 -> rejected_sl record (got {rec.status if rec else 'none'})",
               rec is not None and rec.status == "rejected_sl")

        # --- invalid price (-10015): skipped, no trade record ---
        mt5.force_ticket = -10015
        n_before = len(tm.trades)
        sig = Signal(symbol="XAUUSD", direction="BUY", entry=4400.0,
                     stop_loss=4390.0, take_profits=[4410.0], source_channel="@forexkhan")
        asyncio.run(tm.process_signal(sig))
        mt5.force_ticket = None
        _check(failures, f"-10015 -> no record added ({len(tm.trades) - n_before})",
               len(tm.trades) == n_before)

        # --- wrong-side SL (misparse) rejected before ordering ---
        calls_before = mt5.calls
        sig = Signal(symbol="XAUUSD", direction="SELL", entry=4400.0,
                     stop_loss=4350.0, take_profits=[4380.0], source_channel="@forexkhan")
        asyncio.run(tm.process_signal(sig))
        _check(failures, f"wrong-side SL: no order sent (calls {mt5.calls - calls_before})",
               mt5.calls == calls_before)

        # --- wrong-side TP (misparse) rejected before ordering ---
        calls_before = mt5.calls
        sig = Signal(symbol="XAUUSD", direction="BUY", entry=4400.0,
                     stop_loss=4390.0, take_profits=[4395.0], source_channel="@forexkhan")
        asyncio.run(tm.process_signal(sig))
        _check(failures, f"wrong-side TP: no order sent (calls {mt5.calls - calls_before})",
               mt5.calls == calls_before)

        return failures
    finally:
        os.chdir(prev)


if __name__ == "__main__":
    print("=== entry+SL adjustment test (@Gulljanali17 / @bttesteamin) ===")
    failures = run_scenarios()
    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
