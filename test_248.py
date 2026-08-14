"""
Test for the 248 lot-doubling system.

Verifies that when a FILLED position closes:
  - SL hit  -> per-channel lot multiplier advances (doubling / Fibonacci)
  - TP hit  -> multiplier resets to 1x
  - breakeven close (|profit| < $0.50) -> NO doubling
so the NEXT order's lot size actually grows after each loss.

Pure mocks — no MT5 connection, no Telegram, no network.
Run:  python test_248.py
"""

import sys
import os
import json
import asyncio
import tempfile


# ---------------------------------------------------------------------------
# Inject a FAKE MetaTrader5 module BEFORE importing trade_manager.
# trade_manager._check_deal_history does `import MetaTrader5 as mt5`, so we
# replace it with a controllable fake.
# ---------------------------------------------------------------------------
class FakeDeal:
    def __init__(self, position_id, order, profit, entry=1,
                 price=0.0, commission=0.0, swap=0.0):
        self.position_id = position_id
        self.order = order
        self.profit = profit
        self.entry = entry            # 1 == DEAL_ENTRY_OUT (closing leg)
        self.price = price
        self.commission = commission
        self.swap = swap


class FakeMT5Module:
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1

    def __init__(self):
        self.deals = []

    def history_deals_get(self, frm, to):
        return list(self.deals)


_FAKE_MT5 = FakeMT5Module()
sys.modules["MetaTrader5"] = _FAKE_MT5

# Make project modules importable when run from anywhere
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                  # noqa: E402
from trade_manager import TradeManager, TradeRecord, TradeStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes for the MT5 connector used by TradeManager
# ---------------------------------------------------------------------------
class FakePosition:
    def __init__(self, ticket, type_=1, symbol="XAUUSD", magic=779900):
        self.ticket = ticket
        self.type = type_            # 0 = BUY, 1 = SELL
        self.symbol = symbol
        self.magic = magic


class FakeMT5Connector:
    def __init__(self):
        self.connected = True
        self.positions = []

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return list(self.positions)

    def get_pending_orders(self):
        return []

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4000.0, 4000.1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_settings(tmpdir, lot=0.01):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@forexkhan", "format": "format5"}],
        "trading": {
            "lot_size": lot,
            "default_tp_index": 2,
            "max_sl_pips": 150,
            "max_daily_sl_pips": 500,
            "bot_active": True,
            "settings_password": "x",
            "mode_248": True,
        },
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def make_trade(ticket, channel, direction="SELL"):
    return TradeRecord(
        ticket=ticket,
        channel=channel,
        symbol="XAUUSD",
        direction=direction,
        entry=4000.0,
        sl=4010.0,
        tp=3990.0,
        tp_index=1,
        lot_size=0.01,
        status=TradeStatus.FILLED.value,
        timestamp="2026-01-01T00:00:00+00:00",
    )


def close_with_loss(ticket, loss=-1.0):
    _FAKE_MT5.deals = [FakeDeal(position_id=ticket, order=ticket, profit=loss,
                                entry=FakeMT5Module.DEAL_ENTRY_OUT, price=4010.0)]


def close_with_win(ticket, win=1.0):
    _FAKE_MT5.deals = [FakeDeal(position_id=ticket, order=ticket, profit=win,
                                entry=FakeMT5Module.DEAL_ENTRY_OUT, price=3990.0)]


def _check(failures, label, cond):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        failures.append(label)


def run_scenarios():
    tmpdir = tempfile.mkdtemp(prefix="xau248_")
    prev_cwd = os.getcwd()
    os.chdir(tmpdir)  # keep data/trades.json etc. inside the temp dir
    failures = []

    try:
        settings = make_settings(tmpdir)
        mt5 = FakeMT5Connector()
        tm = TradeManager(settings=settings, mt5=mt5)
        # No file persistence during the test, and no stale records
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None
        tm.trades = []

        def lot_for(ch):
            return round(settings.lot_size * settings.get_248_multiplier(ch), 2)

        # ===== Channel: @forexkhan (doubling sequence 1,2,4,8,...) =====
        ch = "@forexkhan"

        # 1) Trade filled and still open -> no change, mult stays 1x
        ticket = 10001
        t = make_trade(ticket, ch)
        tm.trades.append(t)
        mt5.positions = [FakePosition(ticket, type_=1)]
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"{ch} while open: mult==1x",
               settings.get_248_multiplier(ch) == 1.0)

        # 2) SL hit -> 2x
        mt5.positions = []
        close_with_loss(ticket, -1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"{ch} after 1st SL: mult==2x, lot==0.02",
               settings.get_248_multiplier(ch) == 2.0 and lot_for(ch) == 0.02)
        _check(failures, f"{ch} after 1st SL: status==SL_HIT",
               t.status == TradeStatus.SL_HIT.value)

        # 3) 2nd SL -> 4x
        ticket = 10002
        t2 = make_trade(ticket, ch)
        tm.trades.append(t2)
        mt5.positions = []
        close_with_loss(ticket, -1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"{ch} after 2nd SL: mult==4x, lot==0.04",
               settings.get_248_multiplier(ch) == 4.0 and lot_for(ch) == 0.04)

        # 4) 3rd SL -> 8x
        ticket = 10003
        t3 = make_trade(ticket, ch)
        tm.trades.append(t3)
        mt5.positions = []
        close_with_loss(ticket, -1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"{ch} after 3rd SL: mult==8x, lot==0.08",
               settings.get_248_multiplier(ch) == 8.0 and lot_for(ch) == 0.08)

        # 5) TP hit -> reset to 1x
        ticket = 10004
        t4 = make_trade(ticket, ch)
        tm.trades.append(t4)
        mt5.positions = []
        close_with_win(ticket, 1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"{ch} after TP: mult reset==1x, lot==0.01",
               settings.get_248_multiplier(ch) == 1.0 and lot_for(ch) == 0.01)
        _check(failures, f"{ch} after TP: status==TP_HIT",
               t4.status == TradeStatus.TP_HIT.value)

        # 6) Breakeven close (|profit| < $0.50) -> NO doubling
        ticket = 10005
        t5 = make_trade(ticket, ch)
        tm.trades.append(t5)
        mt5.positions = []
        close_with_loss(ticket, -0.10)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"{ch} breakeven close: mult stays==1x",
               settings.get_248_multiplier(ch) == 1.0)
        _check(failures, f"{ch} breakeven close: status==SL_HIT (closed, but no double)",
               t5.status == TradeStatus.SL_HIT.value)

        # ===== Channel: @Gulljanali17 (standard doubling 1,2,4,8,... like all channels) =====
        chf = "@Gulljanali17"
        for i, exp in enumerate([2.0, 4.0, 8.0, 16.0], start=1):
            ticket = 20000 + i
            tf = make_trade(ticket, chf)
            tm.trades.append(tf)
            mt5.positions = []
            close_with_loss(ticket, -1.0)
            asyncio.run(tm.check_trade_updates())
            _check(failures, f"{chf} after SL #{i}: mult=={exp}, lot=={lot_for(chf)}",
                   settings.get_248_multiplier(chf) == exp)

        return failures
    finally:
        os.chdir(prev_cwd)


if __name__ == "__main__":
    print("=== 248 lot-doubling test ===")
    failures = run_scenarios()
    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
