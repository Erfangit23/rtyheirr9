"""
Test: @BrianTradingForex dual-entry 248 behavior.

Rules under test:
  - Closer-entry leg (TP1, smaller TP) uses the 248 lot (doubles on SL).
  - Farther-entry leg (TP2, bigger RR) is a FLAT base lot and does NOT drive
    the 248 multiplier (its SL/TP never advances/resets the multiplier).

Pure mocks — no MT5 connection, no Telegram, no network.
Run:  python test_dual_248.py
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

from settings import Settings                  # noqa: E402
from signal_parser import Signal               # noqa: E402
from trade_manager import TradeManager, TradeRecord, TradeStatus  # noqa: E402


# ---- Mock that records the lot of each place_limit_order call ----
class PlaceMT5:
    def __init__(self):
        self.placed = []          # lot_size per call
        self._n = 5001

    def ensure_connected(self):
        return True

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4062.0, 4062.5)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.placed.append(lot_size)
        t = self._n
        self._n += 1
        return t


# ---- Mock for closure lifecycle (positions + deals) ----
class FakePos:
    def __init__(self, ticket, type_=0, symbol="XAUUSD", magic=779900):
        self.ticket = ticket
        self.type = type_
        self.symbol = symbol
        self.magic = magic


class CloseMT5:
    def __init__(self):
        self.positions = []

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return list(self.positions)

    def get_pending_orders(self):
        return []

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4060.0, 4060.1)


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@BrianTradingForex", "format": "format4"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "bot_active": True,
                    "settings_password": "x", "mode_248": True},
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


def test_placement_lots(tmpdir):
    print("-- placement lots --")
    failures = []
    settings = make_settings(tmpdir)
    settings.advance_248_step("@BrianTradingForex")  # multiplier 1x -> 2x
    mt5 = PlaceMT5()
    tm = TradeManager(settings=settings, mt5=mt5)
    tm._save_trades = lambda *a, **k: None
    tm._save_linked_orders = lambda *a, **k: None

    sig = Signal(symbol="XAUUSD", direction="BUY", entry=4063.0, stop_loss=4057.0,
                 take_profits=[4068.0, 4085.0], source_channel="@BrianTradingForex",
                 entries=[4060.0, 4063.0])
    asyncio.run(tm.process_signal(sig))

    _check(failures, "two orders placed", len(mt5.placed) == 2)
    _check(failures, "closer(TP1) leg lot == 0.02 (248 x2)",
           len(mt5.placed) >= 1 and mt5.placed[0] == 0.02)
    _check(failures, "farther(TP2) leg lot == 0.01 (flat)",
           len(mt5.placed) >= 2 and mt5.placed[1] == 0.01)
    return failures


def test_closure_isolation(tmpdir):
    print("-- closure multiplier isolation --")
    failures = []
    settings = make_settings(tmpdir)
    mt5 = CloseMT5()
    tm = TradeManager(settings=settings, mt5=mt5)
    tm._save_trades = lambda *a, **k: None
    tm._save_linked_orders = lambda *a, **k: None
    tm.trades = []

    ch = "@BrianTradingForex"

    def close_leg(ticket, participates):
        rec = TradeRecord(ticket=ticket, channel=ch, symbol="XAUUSD", direction="BUY",
                          entry=4063.0, sl=4057.0, tp=4067.0, tp_index=1, lot_size=0.01,
                          status=TradeStatus.FILLED.value,
                          timestamp="2026-01-01T00:00:00+00:00",
                          participates_248=participates)
        tm.trades.append(rec)
        mt5.positions = []
        _FAKE.deals = [FakeDeal(position_id=ticket, order=ticket, profit=-1.0,
                                entry=FakeMT5Module.DEAL_ENTRY_OUT, price=4057.0)]
        asyncio.run(tm.check_trade_updates())

    _check(failures, "start mult == 1x",
           settings.get_248_multiplier(ch) == 1.0)

    close_leg(6001, participates=False)  # farther/bigger-RR leg SL
    _check(failures, "after farther(bigger-RR) SL: mult still == 1x",
           settings.get_248_multiplier(ch) == 1.0)

    close_leg(6002, participates=True)   # closer/TP1 leg SL
    _check(failures, "after closer(TP1) SL: mult == 2x",
           settings.get_248_multiplier(ch) == 2.0)

    close_leg(6003, participates=False)  # another farther SL
    _check(failures, "after 2nd farther SL: mult still == 2x",
           settings.get_248_multiplier(ch) == 2.0)

    close_leg(6004, participates=True)   # another closer SL
    _check(failures, "after 2nd closer SL: mult == 4x",
           settings.get_248_multiplier(ch) == 4.0)

    return failures


def main():
    all_fail = []
    for runner, name in [(test_placement_lots, "place"),
                         (test_closure_isolation, "close")]:
        tmp = tempfile.mkdtemp(prefix=f"xaudual_{name}_")
        prev = os.getcwd()
        os.chdir(tmp)
        try:
            all_fail += runner(tmp)
        finally:
            os.chdir(prev)

    print("=" * 40)
    if all_fail:
        print(f"RESULT: {len(all_fail)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== BrianTradingForex dual-entry 248 test ===")
    main()
