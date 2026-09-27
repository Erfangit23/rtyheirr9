"""
Test: @BrianTradingForex dual-entry placement (base lot per leg).

Replaces the old test_dual_248.py (lot-doubling was removed from the bot).

Verifies, with pure mocks (no MT5 / Telegram / network):
  - a dual signal places exactly 2 orders, each at the base lot (0.01)
  - the wide-SL bypass still works (660-pip SL is not rejected)
  - the closer entry fills first and takes TP1; the farther leg takes TP2
  - the two orders are linked for the breakeven rule

Run:  python test_dual_entry.py
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


class PlaceMT5:
    """Records each place_limit_order call (signal + lot)."""

    def __init__(self):
        self.calls = []           # list of (signal, lot_size)
        self._n = 5001

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return []

    def get_pending_orders(self):
        return []

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4062.0, 4062.5)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        # Mimic the real mt5_connector SL-distance cap so the Brian bypass is testable.
        sl_pips = abs(signal.entry - signal.stop_loss) / 0.1
        if sl_pips > max_sl_pips:
            self.calls.append((signal, None))
            return -1
        self.calls.append((signal, lot_size))
        t = self._n
        self._n += 1
        return t


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@BrianTradingForex", "format": "format4"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "max_open_trades": 5,
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


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xaudual_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    try:
        settings = make_settings(tmpdir)
        mt5 = PlaceMT5()
        tm = TradeManager(settings=settings, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None

        # --- BUY dual entry: 2 orders, both base lot ---
        sig = Signal(symbol="XAUUSD", direction="BUY", entry=4063.0, stop_loss=4057.0,
                     take_profits=[4068.0, 4085.0], source_channel="@BrianTradingForex",
                     entries=[4060.0, 4063.0])
        asyncio.run(tm.process_signal(sig))

        lots = [lot for _, lot in mt5.calls]
        _check(failures, f"two orders placed (got {len(lots)})", len(lots) == 2)
        _check(failures, f"both legs at base lot 0.01 (got {lots})",
               lots == [0.01, 0.01])
        _check(failures, f"two trade records (got {len(tm.trades)})",
               len(tm.trades) == 2)
        _check(failures, "both records lot_size 0.01",
               all(r.lot_size == 0.01 for r in tm.trades))
        _check(failures, f"orders linked for breakeven (got {len(tm._linked_orders)})",
               len(tm._linked_orders) == 1)
        if tm._linked_orders:
            link = next(iter(tm._linked_orders.values()))
            # entries are pulled 5 pips toward market: 4060 -> 4060.5
            # (4063.5 would cross the ask 4062.5, so 4063 stays put)
            _check(failures, f"breakeven price == farther leg entry 4060.5 "
                             f"(got {link['breakeven_price']})",
                   link["breakeven_price"] == 4060.5)

        # closer-to-market entry should be the "first" (TP1) leg
        entries = sorted((c[0].entry, c[0].take_profits[0]) for c in mt5.calls)
        closer_entry, closer_tp = entries[-1]      # BUY: higher entry = closer
        _check(failures, f"closer leg gets TP1 4067 (got {closer_tp})",
               closer_tp == 4067.0)

        # --- SELL dual entry with a wide (660-pip) SL: bypass must still work ---
        mt5.calls.clear()
        tm.trades.clear()
        tm._linked_orders.clear()
        sig2 = Signal(symbol="XAUUSD", direction="SELL", entry=4371.0, stop_loss=4437.0,
                      take_profits=[4361.0, 4341.0], source_channel="@BrianTradingForex",
                      entries=[4371.0, 4374.0])
        asyncio.run(tm.process_signal(sig2))
        lots2 = [lot for _, lot in mt5.calls]
        _check(failures, f"wide SL: both legs placed, base lot (got {lots2})",
               lots2 == [0.01, 0.01])
        _check(failures, "wide SL: no leg rejected with None",
               None not in lots2)
    finally:
        os.chdir(prev)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== BrianTradingForex dual-entry test ===")
    main()
