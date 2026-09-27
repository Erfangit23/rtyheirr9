"""
Test: trade closure lifecycle (status marking + per-channel SL cooldown).

Replaces the old test_248.py (lot-doubling was removed from the bot).

Verifies, with pure mocks (no MT5 / Telegram / network):
  - a filled position that disappears with a losing deal -> SL_HIT
  - a filled position that disappears with a winning deal -> TP_HIT
  - a breakeven close (|profit| < $0.50) -> SL_HIT status but NO cooldown
  - a real SL on @Gulljanali17 -> 90-min cooldown registered
  - channels are independent (a loss on one channel doesn't pause another)

Run:  python test_closure.py
"""

import sys
import os
import json
import asyncio
import tempfile


# ---- Fake MetaTrader5 module (used by trade_manager._check_deal_history) ----
class FakeDeal:
    def __init__(self, position_id, order, profit, entry=1, price=0.0,
                 commission=0.0, swap=0.0):
        self.position_id = position_id
        self.order = order
        self.profit = profit
        self.entry = entry            # 1 == DEAL_ENTRY_OUT
        self.price = price
        self.commission = commission
        self.swap = swap


class FakeMT5Module:
    DEAL_ENTRY_OUT = 1

    def __init__(self):
        self.deals = []

    def history_deals_get(self, frm, to):
        return list(self.deals)


_FAKE = FakeMT5Module()
sys.modules["MetaTrader5"] = _FAKE

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                                  # noqa: E402
from trade_manager import TradeManager, TradeRecord, TradeStatus  # noqa: E402


class FakePos:
    def __init__(self, ticket, type_=0, symbol="XAUUSD", magic=779900):
        self.ticket = ticket
        self.type = type_
        self.symbol = symbol
        self.magic = magic


class FakeConn:
    def __init__(self):
        self.positions = []
        self.orders = []

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return list(self.positions)

    def get_pending_orders(self):
        return list(self.orders)

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4000.0, 4000.1)


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@Gulljanali17", "format": "format3"},
                     {"id": "@forexkhan", "format": "format5"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "max_open_trades": 5,
                    "bot_active": True, "settings_password": "x"},
        "validation": {"mode": "off"},
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def make_trade(ticket, channel, direction="SELL"):
    return TradeRecord(
        ticket=ticket, channel=channel, symbol="XAUUSD", direction=direction,
        entry=4000.0, sl=4010.0, tp=3990.0, tp_index=1, lot_size=0.01,
        status=TradeStatus.FILLED.value, timestamp="2026-01-01T00:00:00+00:00",
    )


def close_with(ticket, profit):
    _FAKE.deals = [FakeDeal(position_id=ticket, order=ticket, profit=profit,
                            entry=FakeMT5Module.DEAL_ENTRY_OUT, price=4000.0)]


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xauclose_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    try:
        settings = make_settings(tmpdir)
        conn = FakeConn()
        tm = TradeManager(settings=settings, mt5=conn)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None
        tm._save_sl_cooldown = lambda *a, **k: None
        tm.trades = []

        # --- real SL -> SL_HIT + cooldown (Gulljanali is a cooldown channel) ---
        t = make_trade(10001, "@Gulljanali17")
        tm.trades.append(t)
        close_with(10001, -1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"real SL -> status SL_HIT (got {t.status})",
               t.status == TradeStatus.SL_HIT.value)
        _check(failures, "real SL on @Gulljanali17 -> cooldown active",
               tm._cooldown_remaining_min("@Gulljanali17") > 0)

        # --- TP -> TP_HIT ---
        t2 = make_trade(10002, "@forexkhan")
        tm.trades.append(t2)
        close_with(10002, 1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"TP -> status TP_HIT (got {t2.status})",
               t2.status == TradeStatus.TP_HIT.value)

        # --- breakeven close: status SL_HIT but no cooldown ---
        tm._sl_cooldown = {}   # clear the earlier Gulljanali cooldown
        t3 = make_trade(10003, "@Gulljanali17")
        tm.trades.append(t3)
        close_with(10003, -0.10)
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"breakeven close -> status SL_HIT (got {t3.status})",
               t3.status == TradeStatus.SL_HIT.value)
        _check(failures, "breakeven close -> NO cooldown",
               tm._cooldown_remaining_min("@Gulljanali17") == 0.0)

        # --- channels are independent: forexkhan SL doesn't pause Gulljanali ---
        tm._sl_cooldown = {}
        t4 = make_trade(10004, "@forexkhan")
        tm.trades.append(t4)
        close_with(10004, -1.0)
        asyncio.run(tm.check_trade_updates())
        _check(failures, "@forexkhan SL -> no cooldown on @Gulljanali17",
               tm._cooldown_remaining_min("@Gulljanali17") == 0.0)

        # --- position still open: nothing changes ---
        t5 = make_trade(10005, "@forexkhan")
        tm.trades.append(t5)
        conn.positions = [FakePos(10005, type_=1)]
        _FAKE.deals = []
        asyncio.run(tm.check_trade_updates())
        _check(failures, "still-open position keeps FILLED status",
               t5.status == TradeStatus.FILLED.value)

        # --- order vanished with no deal history -> CANCELLED (stops retrying) ---
        t6 = make_trade(10006, "@forexkhan")
        tm.trades.append(t6)
        conn.positions = []
        _FAKE.deals = []
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"no deal history -> CANCELLED (got {t6.status})",
               t6.status == TradeStatus.CANCELLED.value)
    finally:
        os.chdir(prev)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== trade closure lifecycle test ===")
    main()
