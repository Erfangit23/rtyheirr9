"""
Test: pre-live safety guards in TradeManager.process_signal.

Verifies, with pure mocks (no MT5 / Telegram / network):
  - daily loss cap: new signals are blocked once today's realised loss
    reaches max_daily_sl_pips (and recorded as rejected_daily)
  - daily loss cap disabled with max_daily_sl_pips = 0
  - duplicate guard: a re-posted call whose order is still pending is ignored
    (no second order, no extra record), while a different channel / a
    different entry / an old order are still allowed
  - symbol normalisation to the broker's configured symbol name
  - a signal with no take-profit levels is ignored (never crashes)

Run:  python test_safety_guards.py
"""

import sys
import os
import json
import asyncio
import tempfile
from datetime import datetime, timezone, timedelta

sys.modules.setdefault("MetaTrader5", type(sys)("MetaTrader5"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                  # noqa: E402
from signal_parser import Signal               # noqa: E402
from trade_manager import TradeManager, TradeStatus  # noqa: E402


class GuardMT5:
    """Mock connector: settable daily loss, records placed signals."""

    def __init__(self, loss_pips=0.0):
        self.loss_pips = loss_pips
        self.placed = []
        self._n = 8001

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return []

    def get_pending_orders(self):
        return []

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_today_loss_pips(self):
        return self.loss_pips

    def get_symbol_price(self, symbol=None):
        return (4062.0, 4062.5)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.placed.append(signal)
        t = self._n
        self._n += 1
        return t


def make_settings(tmpdir, max_daily=500, max_open=5, symbol="XAUUSD"):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": symbol},
        "channels": [{"id": "@forexkhan", "format": "format5"},
                     {"id": "@GoldVisionofficial", "format": "format7"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": max_daily, "max_open_trades": max_open,
                    "bot_active": True, "settings_password": "x"},
        "validation": {"mode": "off"},
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def sell(entry=4060.0, channel="@forexkhan"):
    return Signal(symbol="XAUUSD", direction="SELL", entry=entry, stop_loss=entry + 10.0,
                  take_profits=[entry - 10.0, entry - 20.0], source_channel=channel)


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xauguard_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    try:
        # ---------------- daily loss cap ----------------
        print("-- daily loss cap --")
        s = make_settings(tmpdir, max_daily=500)
        mt5 = GuardMT5(loss_pips=400.0)
        tm = TradeManager(settings=s, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None
        tm.trades = []

        asyncio.run(tm.process_signal(sell(4060.0)))
        _check(failures, f"400/500 pips lost: order still placed (got {len(mt5.placed)})",
               len(mt5.placed) == 1)

        mt5.loss_pips = 500.0
        n_before = len(tm.trades)
        asyncio.run(tm.process_signal(sell(4070.0)))
        _check(failures, f"500/500 pips lost: order BLOCKED (got {len(mt5.placed)})",
               len(mt5.placed) == 1)
        _check(failures, f"blocked trade recorded as rejected_daily (got {tm.trades[-1].status})",
               len(tm.trades) == n_before + 1
               and tm.trades[-1].status == TradeStatus.REJECTED_DAILY.value)

        mt5.loss_pips = 9999.0
        s.set_max_daily_sl_pips(0)          # cap disabled
        asyncio.run(tm.process_signal(sell(4080.0)))
        _check(failures, f"cap 0 (disabled): order placed (got {len(mt5.placed)})",
               len(mt5.placed) == 2)
        s.set_max_daily_sl_pips(500)

        # ---------------- duplicate guard ----------------
        print("-- duplicate signal guard --")
        s2 = make_settings(tmpdir, max_daily=0)
        mt5b = GuardMT5()
        tm2 = TradeManager(settings=s2, mt5=mt5b)
        tm2._save_trades = lambda *a, **k: None
        tm2._save_linked_orders = lambda *a, **k: None
        tm2.trades = []

        asyncio.run(tm2.process_signal(sell(4060.0)))
        _check(failures, f"first signal placed (got {len(mt5b.placed)})",
               len(mt5b.placed) == 1)
        n_records = len(tm2.trades)

        asyncio.run(tm2.process_signal(sell(4060.0)))     # exact repost
        _check(failures, f"exact repost ignored (got {len(mt5b.placed)})",
               len(mt5b.placed) == 1)
        _check(failures, f"repost adds no trade record (got {len(tm2.trades)})",
               len(tm2.trades) == n_records)

        asyncio.run(tm2.process_signal(sell(4060.4)))     # within 10 pips
        _check(failures, f"near-identical repost ignored (got {len(mt5b.placed)})",
               len(mt5b.placed) == 1)

        asyncio.run(tm2.process_signal(sell(4090.0)))     # clearly different level
        _check(failures, f"different entry allowed (got {len(mt5b.placed)})",
               len(mt5b.placed) == 2)

        asyncio.run(tm2.process_signal(sell(4060.0, channel="@GoldVisionofficial")))
        _check(failures, f"other channel allowed (got {len(mt5b.placed)})",
               len(mt5b.placed) == 3)

        # an OLD pending order no longer blocks a repost
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        tm2.trades[0].timestamp = old_ts
        asyncio.run(tm2.process_signal(sell(4060.0)))
        _check(failures, f"repost after 90 min allowed (got {len(mt5b.placed)})",
               len(mt5b.placed) == 4)

        # ---------------- symbol normalisation ----------------
        print("-- symbol normalisation --")
        s3 = make_settings(tmpdir, max_daily=0, symbol="XAUUSD.pro")
        mt5c = GuardMT5()
        tm3 = TradeManager(settings=s3, mt5=mt5c)
        tm3._save_trades = lambda *a, **k: None
        tm3._save_linked_orders = lambda *a, **k: None
        tm3.trades = []
        asyncio.run(tm3.process_signal(sell(4060.0)))
        _check(failures, f"order uses broker symbol (got {mt5c.placed[0].symbol if mt5c.placed else '?'})",
               mt5c.placed and mt5c.placed[0].symbol == "XAUUSD.pro")
        _check(failures, f"record stores broker symbol (got {tm3.trades[-1].symbol})",
               tm3.trades[-1].symbol == "XAUUSD.pro")

        # ---------------- empty take-profits ----------------
        print("-- empty TP guard --")
        s4 = make_settings(tmpdir, max_daily=0)
        mt5d = GuardMT5()
        tm4 = TradeManager(settings=s4, mt5=mt5d)
        tm4._save_trades = lambda *a, **k: None
        tm4._save_linked_orders = lambda *a, **k: None
        tm4.trades = []
        bad = Signal(symbol="XAUUSD", direction="SELL", entry=4060.0,
                     stop_loss=4070.0, take_profits=[], source_channel="@forexkhan")
        asyncio.run(tm4.process_signal(bad))
        _check(failures, f"no TPs: nothing placed (got {len(mt5d.placed)})",
               len(mt5d.placed) == 0)
        _check(failures, "no TPs: no record added", len(tm4.trades) == 0)
    finally:
        os.chdir(prev)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== pre-live safety guards test ===")
    main()
