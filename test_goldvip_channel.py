"""
Test: @goldviptraderyy_7 (format9) — dual entry with breakeven, no Brian tweaks.

Message format:
    GOLD BUY 4153.4150
    TP 4160
    TP 4170
    TP 4190
    SL 4144

Rules under test:
  - the dot-separated range gives TWO entries (4153 and 4150)
  - two orders are placed: the entry closer to the market takes TP1, the
    farther one takes TP2
  - the two orders are linked so that when the closer leg hits TP1 the other
    leg's SL moves to its own entry (risk-free)
  - the channel's own levels are used UNCHANGED (no Brian 5-pip pull, no
    TP1 -10 pips, no 150-pip TP2 cap)
  - @BrianTradingForex still gets its own adjustments (regression)
  - the global SL cap still applies to this channel (only Brian bypasses it)
  - the parser must not hijack other formats' single-price messages

Pure mocks — no MT5, no Telegram, no network.
Run:  python test_goldvip_channel.py
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
from signal_parser import parse_signal, parse_format9, Signal  # noqa: E402
from trade_manager import TradeManager         # noqa: E402

CHANNEL = "@goldviptraderyy_7"
MESSAGE = """GOLD BUY 4153.4150
TP 4160
TP 4170
TP 4190
SL 4144"""


class PlaceMT5:
    """Records each place_limit_order call and mimics the connector's SL cap."""

    def __init__(self, bid=4160.0, ask=4160.5):
        self.bid, self.ask = bid, ask
        self.calls = []          # (signal, lot_size)
        self._n = 6001

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return []

    def get_pending_orders(self):
        return []

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol=None):
        return (self.bid, self.ask)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        sl_pips = abs(signal.entry - signal.stop_loss) / 0.1
        if sl_pips > max_sl_pips:
            self.calls.append((signal, None))      # rejected: SL too large
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
        "channels": [{"id": CHANNEL, "format": "format9"},
                     {"id": "@BrianTradingForex", "format": "format4"}],
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
    tmpdir = tempfile.mkdtemp(prefix="xaugvip_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    try:
        # ---------------- parser ----------------
        print("-- parser --")
        sig = parse_signal(MESSAGE, CHANNEL, "format9")
        _check(failures, f"parses (got {sig})", sig is not None)
        _check(failures, f"BUY (got {sig.direction})", sig.direction == "BUY")
        _check(failures, f"two entries [4153, 4150] (got {sig.entries})",
               sig.entries == [4153.0, 4150.0])
        _check(failures, f"three TPs [4160, 4170, 4190] (got {sig.take_profits})",
               sig.take_profits == [4160.0, 4170.0, 4190.0])
        _check(failures, f"SL 4144 (got {sig.stop_loss})", sig.stop_loss == 4144.0)
        _check(failures, "auto mode also finds it",
               parse_signal(MESSAGE, CHANNEL, "auto") is not None)

        # it must not hijack other formats' single-price messages
        other3 = "XAUUSD Sell 4064\nTP 4059\nTP 4054\nSL 4074"
        other8 = "GOLD Buy Limit 4252-4253\nTP 4259\nSL 4246"
        # the parser itself must not hijack other formats (parse_signal would
        # legitimately fall back to auto mode, so test the parser directly)
        _check(failures, "parser rejects a format3 message",
               parse_format9(other3, "@test") is None)
        _check(failures, "parser rejects a format8 message",
               parse_format9(other8, "@test") is None)

        # ---------------- placement: two orders, raw levels ----------------
        print("-- dual entry placement --")
        s = make_settings(tmpdir)
        mt5 = PlaceMT5(bid=4160.0, ask=4160.5)
        tm = TradeManager(settings=s, mt5=mt5)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None
        tm.trades = []
        tm._linked_orders = {}

        asyncio.run(tm.process_signal(parse_signal(MESSAGE, CHANNEL, "format9")))

        lots = [lot for _s, lot in mt5.calls]
        _check(failures, f"two orders placed (got {len(lots)})", len(lots) == 2)
        _check(failures, f"both at base lot 0.01 (got {lots})", lots == [0.01, 0.01])

        legs = [(s_.entry, s_.take_profits[0]) for s_, _l in mt5.calls]
        _check(failures, f"closer entry 4153 -> TP1 4160, farther 4150 -> TP2 4170 (got {legs})",
               legs == [(4153.0, 4160.0), (4150.0, 4170.0)])
        _check(failures, "levels used UNCHANGED (no Brian 5-pip pull / TP1 -10)",
               all(e in (4153.0, 4150.0) for e, _t in legs)
               and all(t in (4160.0, 4170.0) for _e, t in legs))
        _check(failures, "SL passed through unchanged (4144)",
               all(s_.stop_loss == 4144.0 for s_, _l in mt5.calls))

        _check(failures, f"legs linked for breakeven (got {len(tm._linked_orders)})",
               len(tm._linked_orders) == 1)
        if tm._linked_orders:
            link = next(iter(tm._linked_orders.values()))
            _check(failures, f"breakeven price = farther entry 4150 (got {link['breakeven_price']})",
                   link["breakeven_price"] == 4150.0)

        _check(failures, "two trade records", len(tm.trades) == 2)
        _check(failures, "records carry tp2 for the standard cancel rule",
               all(t.tp2 == 4170.0 for t in tm.trades))

        # ---------------- SELL direction ----------------
        print("-- SELL: lower entry is closer --")
        sell_msg = """GOLD SELL 4153.4150
TP 4145
TP 4135
SL 4160"""
        mt5.calls.clear()
        tm.trades.clear()
        tm._linked_orders.clear()
        asyncio.run(tm.process_signal(parse_signal(sell_msg, CHANNEL, "format9")))
        legs = [(s_.entry, s_.take_profits[0]) for s_, _l in mt5.calls]
        # market is above both entries -> the LOWER entry (4150) is closer
        _check(failures, f"SELL closer 4150 -> TP1 4145 (got {legs})",
               legs == [(4150.0, 4145.0), (4153.0, 4135.0)])

        # ---------------- Brian regression ----------------
        print("-- @BrianTradingForex keeps its own tweaks --")
        mt5.calls.clear()
        tm.trades.clear()
        tm._linked_orders.clear()
        brian = Signal(symbol="XAUUSD", direction="BUY", entry=4063.0, stop_loss=4057.0,
                       take_profits=[4068.0, 4085.0], source_channel="@BrianTradingForex",
                       entries=[4060.0, 4063.0])
        asyncio.run(tm.process_signal(brian))
        b_legs = [(s_.entry, s_.take_profits[0]) for s_, _l in mt5.calls]
        # entries pulled +5 pips; closer leg (4063.5) -> TP1 4068-10p = 4067;
        # farther leg (4060.5) -> TP2 = 150-pip cap from ITS OWN entry (4075.5)
        # minus the 10-pip pull = 4074.5 (i.e. 140 pips, was 170 before the fix)
        _check(failures, f"Brian keeps its pull + TP tweaks (got {b_legs})",
               b_legs == [(4063.5, 4067.0), (4060.5, 4074.5)])
        _check(failures, "Brian's far leg is capped near 150 pips, not 170",
               abs(4060.5 - 4074.5) / 0.1 <= 150)

        # ---------------- SL cap applies here, not to Brian ----------------
        print("-- SL cap --")
        wide = Signal(symbol="XAUUSD", direction="SELL", entry=4371.0, stop_loss=4437.0,
                      take_profits=[4361.0, 4341.0], source_channel=CHANNEL,
                      entries=[4371.0, 4374.0])
        mt5.calls.clear()
        tm.trades.clear()
        tm._linked_orders.clear()
        asyncio.run(tm.process_signal(wide))
        _check(failures, "660-pip SL is REJECTED for this channel",
               all(lot is None for _s, lot in mt5.calls) and len(mt5.calls) == 2)

        wide_brian = Signal(symbol="XAUUSD", direction="SELL", entry=4371.0, stop_loss=4437.0,
                            take_profits=[4361.0, 4341.0], source_channel="@BrianTradingForex",
                            entries=[4371.0, 4374.0])
        mt5.calls.clear()
        tm.trades.clear()
        tm._linked_orders.clear()
        asyncio.run(tm.process_signal(wide_brian))
        _check(failures, "same SL is placed for Brian (cap bypass)",
               all(lot == 0.01 for _s, lot in mt5.calls) and len(mt5.calls) == 2)
    finally:
        os.chdir(prev)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== @goldviptraderyy_7 dual-entry test ===")
    main()
