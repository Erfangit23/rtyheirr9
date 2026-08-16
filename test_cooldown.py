"""
Test: @Gulljanali17 SL cooldown (90-minute pause after a real SL hit).

Rules under test:
- After an SL hit on @Gulljanali17, new signals from that channel are IGNORED
  (no order placed) until 90 minutes have passed.
- After the window expires, the channel trades again.
- Breakeven closes (|profit| < $0.50) do NOT start a cooldown.
- Works with 248 mode OFF (cooldown is independent of 248).
- Another channel's SL does not pause @Gulljanali17.
- Cooldown state persists to data/sl_cooldown.json (restart-safe).

Pure mocks — no MT5 connection, no Telegram, no network.
Run:  python test_cooldown.py
"""

import sys
import os
import json
import asyncio
import tempfile
from datetime import datetime, timezone, timedelta

# ---- Fake MetaTrader5 module (used by _check_deal_history) ----
class FakeDeal:
    def __init__(self, position_id, order, profit, entry=1, price=0.0):
        self.position_id = position_id
        self.order = order
        self.profit = profit
        self.entry = entry  # 1 == DEAL_ENTRY_OUT
        self.price = price
        self.commission = 0.0
        self.swap = 0.0


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
from trade_manager import TradeManager         # noqa: E402


class FakePosition:
    def __init__(self, ticket, type_=1, symbol="XAUUSD", magic=779900):
        self.ticket = ticket
        self.type = type_
        self.symbol = symbol
        self.magic = magic


class FakeMT5Connector:
    def __init__(self):
        self.connected = True
        self.positions = []
        self.placed = []      # lot sizes of place_limit_order calls
        self._n = 30001

    def ensure_connected(self):
        return True

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4404.0, 4405.0)

    def get_open_positions(self):
        return list(self.positions)

    def get_pending_orders(self):
        return []

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.placed.append(lot_size)
        t = self._n
        self._n += 1
        return t


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@Gulljanali17", "format": "format3"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "bot_active": True,
                    "settings_password": "x", "mode_248": False},
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


def new_signal(channel="@Gulljanali17"):
    return Signal(symbol="XAUUSD", direction="BUY", entry=4400.0,
                  stop_loss=4390.0, take_profits=[4410.0, 4420.0],
                  source_channel=channel)


def run_scenarios():
    tmpdir = tempfile.mkdtemp(prefix="xaucd_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    failures = []

    try:
        settings = make_settings(tmpdir)
        mt5 = FakeMT5Connector()
        tm = TradeManager(settings=settings, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None
        tm.trades = []

        # --- 1) place a trade, fill it, then SL it ---
        asyncio.run(tm.process_signal(new_signal()))
        _check(failures, "signal 1 placed", len(mt5.placed) == 1)
        ticket = 30001

        mt5.positions = [FakePosition(ticket)]
        asyncio.run(tm.check_trade_updates())          # -> filled
        mt5.positions = []
        _FAKE.deals = [FakeDeal(ticket, ticket, -1.0)]
        asyncio.run(tm.check_trade_updates())          # -> sl_hit

        trade = next(t for t in tm.trades if t.ticket == ticket)
        _check(failures, f"trade closed as sl_hit (got {trade.status})",
               trade.status == "sl_hit")
        _check(failures, "cooldown registered", "@Gulljanali17" in tm._sl_cooldown)
        _check(failures, "cooldown persisted to file",
               os.path.exists("data/sl_cooldown.json"))

        # --- 2) new signal during cooldown is ignored ---
        asyncio.run(tm.process_signal(new_signal()))
        _check(failures, f"signal during cooldown NOT placed (calls={len(mt5.placed)})",
               len(mt5.placed) == 1)
        _check(failures, "remaining > 89 min",
               tm._cooldown_remaining_min("@Gulljanali17") > 89)

        # --- 3) after the window expires, trading resumes ---
        expired_ts = (datetime.now(timezone.utc) - timedelta(minutes=91)).isoformat()
        tm._sl_cooldown["@Gulljanali17"] = expired_ts
        asyncio.run(tm.process_signal(new_signal()))
        _check(failures, f"signal after cooldown placed (calls={len(mt5.placed)})",
               len(mt5.placed) == 2)

        # --- 4) breakeven close does not start a cooldown ---
        ticket2 = 30002
        mt5.positions = [FakePosition(ticket2)]
        asyncio.run(tm.check_trade_updates())
        mt5.positions = []
        _FAKE.deals = [FakeDeal(ticket2, ticket2, -0.10)]
        tm._sl_cooldown.pop("@Gulljanali17", None)
        asyncio.run(tm.check_trade_updates())
        t2 = next(t for t in tm.trades if t.ticket == ticket2)
        _check(failures, f"breakeven closed as sl_hit (got {t2.status})",
               t2.status == "sl_hit")
        _check(failures, "breakeven did NOT start cooldown",
               "@Gulljanali17" not in tm._sl_cooldown)
        asyncio.run(tm.process_signal(new_signal()))
        _check(failures, f"signal after breakeven placed (calls={len(mt5.placed)})",
               len(mt5.placed) == 3)

        # --- 5) another channel's SL doesn't pause @Gulljanali17 ---
        ticket3 = 40001  # distinct ticket (30003 belongs to signal 3's pending trade)
        tm.trades.append(type(tm.trades[0])(
            ticket=ticket3, channel="@forexkhan", symbol="XAUUSD", direction="SELL",
            entry=4400.0, sl=4410.0, tp=4390.0, tp_index=1, lot_size=0.01,
            status="filled", timestamp="2026-01-01T00:00:00+00:00"))
        _FAKE.deals = [FakeDeal(ticket3, ticket3, -1.0)]
        asyncio.run(tm.check_trade_updates())
        _check(failures, "@forexkhan SL did not pause @Gulljanali17",
               tm._cooldown_remaining_min("@Gulljanali17") == 0.0)
        asyncio.run(tm.process_signal(new_signal()))
        _check(failures, f"Gulljanali still trades (calls={len(mt5.placed)})",
               len(mt5.placed) == 4)

        return failures
    finally:
        os.chdir(prev)


if __name__ == "__main__":
    print("=== SL cooldown test (@Gulljanali17, 90 min) ===")
    failures = run_scenarios()
    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)
