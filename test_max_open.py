"""
Test: max concurrent open trades cap (account safety).

Verifies, with pure mocks (no MT5 / Telegram / network):
  - a signal is placed while open trades < cap
  - a signal is SKIPPED when open trades would exceed the cap
  - the skipped signal is recorded as rejected_max_open (not silently dropped)
  - a dual-entry signal needs 2 free slots (4 open + dual -> skipped)
  - max_open_trades = 0 disables the cap
  - the /settings + `maxtrades` command path updates the cap

Run:  python test_max_open.py
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
from bot_commands import CommandHandler        # noqa: E402


class FakePos:
    def __init__(self, ticket):
        self.ticket = ticket


class CapMT5:
    def __init__(self):
        self.n_positions = 0
        self.n_orders = 0
        self.placed = []
        self._n = 9001

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return [FakePos(1000 + i) for i in range(self.n_positions)]

    def get_pending_orders(self):
        return [FakePos(2000 + i) for i in range(self.n_orders)]

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4062.0, 4062.5)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.placed.append(signal)
        t = self._n
        self._n += 1
        return t


def make_settings(tmpdir, max_open=5):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@forexkhan", "format": "format5"},
                     {"id": "@BrianTradingForex", "format": "format4"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "max_open_trades": max_open,
                    "bot_active": True, "settings_password": "pw"},
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


def single_signal():
    return Signal(symbol="XAUUSD", direction="SELL", entry=4060.0, stop_loss=4070.0,
                  take_profits=[4050.0, 4040.0], source_channel="@forexkhan")


def dual_signal():
    return Signal(symbol="XAUUSD", direction="BUY", entry=4063.0, stop_loss=4057.0,
                  take_profits=[4068.0, 4085.0], source_channel="@BrianTradingForex",
                  entries=[4060.0, 4063.0])


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xaumaxopen_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    try:
        settings = make_settings(tmpdir)
        mt5 = CapMT5()
        tm = TradeManager(settings=settings, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None
        tm.trades = []

        # --- 0 open -> placed ---
        asyncio.run(tm.process_signal(single_signal()))
        _check(failures, f"0 open: order placed (got {len(mt5.placed)})",
               len(mt5.placed) == 1)
        _check(failures, "0 open: record is pending",
               tm.trades[-1].status == "pending")

        # --- 4 open (3 positions + 1 pending) -> placed (5th) ---
        mt5.placed.clear()
        mt5.n_positions, mt5.n_orders = 3, 1
        asyncio.run(tm.process_signal(single_signal()))
        _check(failures, f"4 open: 5th order placed (got {len(mt5.placed)})",
               len(mt5.placed) == 1)

        # --- 5 open -> skipped ---
        mt5.placed.clear()
        n_before = len(tm.trades)
        mt5.n_positions, mt5.n_orders = 4, 1
        asyncio.run(tm.process_signal(single_signal()))
        _check(failures, f"5 open: no order placed (got {len(mt5.placed)})",
               len(mt5.placed) == 0)
        _check(failures, f"5 open: record added as rejected_max_open (got {tm.trades[-1].status})",
               len(tm.trades) == n_before + 1
               and tm.trades[-1].status == "rejected_max_open")

        # --- dual entry needs 2 slots: 4 open -> skipped ---
        mt5.placed.clear()
        mt5.n_positions, mt5.n_orders = 3, 1     # 4 open, dual needs 2 -> 6 > 5
        asyncio.run(tm.process_signal(dual_signal()))
        _check(failures, f"4 open + dual (needs 2): skipped (got {len(mt5.placed)})",
               len(mt5.placed) == 0)

        # --- dual entry with 3 open -> both legs placed ---
        mt5.placed.clear()
        mt5.n_positions, mt5.n_orders = 2, 1     # 3 open, dual -> 5 <= 5
        asyncio.run(tm.process_signal(dual_signal()))
        _check(failures, f"3 open + dual: 2 legs placed (got {len(mt5.placed)})",
               len(mt5.placed) == 2)

        # --- cap disabled (0) -> placed even with 20 open ---
        settings.set_max_open_trades(0)
        mt5.placed.clear()
        mt5.n_positions, mt5.n_orders = 15, 5
        asyncio.run(tm.process_signal(single_signal()))
        _check(failures, f"cap 0 (unlimited): placed (got {len(mt5.placed)})",
               len(mt5.placed) == 1)
        settings.set_max_open_trades(5)
        _check(failures, "cap restored to 5", settings.max_open_trades == 5)

        # --- command path: maxtrades 3 ---
        handler = CommandHandler(settings=settings, mt5=mt5, trade_manager=tm)
        handler._awaiting_password[777] = "authenticated"
        resp = asyncio.run(handler.handle("maxtrades 3", 777))
        _check(failures, f"maxtrades 3 accepted (got {resp[:30]!r})",
               "3" in resp and settings.max_open_trades == 3)
        resp2 = asyncio.run(handler.handle("maxtrades abc", 777))
        _check(failures, "maxtrades abc rejected", "Invalid" in resp2)
        _check(failures, "/settings shows Max Open Trades",
               "Max Open Trades" in handler._cmd_settings())

        # --- full auth flow: /change -> password -> command -> done ---
        h2 = CommandHandler(settings=settings, mt5=mt5, trade_manager=tm)
        r1 = asyncio.run(h2.handle("/change", 888))
        _check(failures, "/change asks for password", "password" in r1.lower())
        r2 = asyncio.run(h2.handle("pw", 888))
        _check(failures, "correct password authenticates", "Authenticated" in r2)
        r3 = asyncio.run(h2.handle("maxtrades 4", 888))
        _check(failures, f"authed command works right after login (got {r3[:35]!r})",
               settings.max_open_trades == 4)
        r4 = asyncio.run(h2.handle("done", 888))
        _check(failures, "done ends the session", "ended" in r4.lower())
        r5 = asyncio.run(h2.handle("maxtrades 9", 888))
        _check(failures, "after done, command is rejected again",
               settings.max_open_trades == 4)
        r6 = asyncio.run(h2.handle("/change", 889))
        r7 = asyncio.run(h2.handle("wrongpass", 889))
        _check(failures, "wrong password denied", "denied" in r7.lower())
    finally:
        os.chdir(prev)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== max concurrent open trades test ===")
    main()
