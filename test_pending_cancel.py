"""
Test: pending-order cancel rules now have a grace period.

The bug (seen live on @BrianTradingForex): the bot reported "dual entry orders
placed" and then, in the very next 5-second cycle, "BOTH orders CANCELLED -
price hit TP2 without filling". The cancel rule is meant for "the move finished
while we were still unfilled", but it was evaluated from the first cycle — so
whenever the price was already at/beyond the channel's TP2 at placement time,
both legs died instantly.

Fix under test: TP_CANCEL_GRACE_MIN — a pending order is never cancelled by the
"price reached the target unfilled" rules during its first minutes.

Verifies, for all three rules (generic TP2, khan TP1, Brian TP2):
  - a freshly placed order is NOT cancelled even when the price is already past
    the level
  - the same order IS cancelled once it is older than the grace period
  - an unreadable timestamp is treated as young (never an instant cancel)
  - Brian cancels the partner leg too, but only after the grace

Run:  python test_pending_cancel.py
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

from settings import Settings                     # noqa: E402
from trade_manager import TradeManager, TradeRecord, TradeStatus   # noqa: E402


class FakePos:
    def __init__(self, ticket, type_=0, symbol="XAUUSD", magic=779900):
        self.ticket = ticket
        self.type = type_
        self.symbol = symbol
        self.magic = magic


class CancelMT5:
    """Price sits beyond every target; every pending order is visible."""

    def __init__(self, bid=4100.0, ask=4101.0):
        self.bid, self.ask = bid, ask
        self.cancelled = []

    def ensure_connected(self):
        return True

    def get_open_positions(self):
        return []

    def get_pending_orders(self):
        return [FakePos(t.ticket, type_=0) for t in _ORDER_BOOK]

    def get_symbol_price(self, symbol=None):
        return (self.bid, self.ask)

    def cancel_order(self, ticket):
        self.cancelled.append(ticket)
        return True


_ORDER_BOOK = []   # tickets the mock reports as still pending


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@BrianTradingForex", "format": "format4"},
                     {"id": "@forexkhan", "format": "format5"},
                     {"id": "@GoLDVipSigbal", "format": "format3"}],
        "trading": {"lot_size": 0.01, "bot_active": True},
        "validation": {"mode": "off"},
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def rec(ticket, channel, direction="BUY", entry=4000.0, tp=4010.0, sl=3990.0,
        tp2=4090.0, minutes_old=0.0, timestamp=None):
    ts = timestamp or (datetime.now(timezone.utc)
                       - timedelta(minutes=minutes_old)).isoformat()
    return TradeRecord(ticket=ticket, channel=channel, symbol="XAUUSD",
                       direction=direction, entry=entry, sl=sl, tp=tp, tp_index=1,
                       lot_size=0.01, status=TradeStatus.PENDING.value,
                       timestamp=ts, tp2=tp2)


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xaucancel_")
    prev = os.getcwd()
    os.chdir(tmpdir)
    try:
        tm = TradeManager(settings=make_settings(tmpdir), mt5=CancelMT5())
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None

        # ---------------- generic channel (TP2 rule) ----------------
        print("-- generic channel: cancel at TP2 --")
        global _ORDER_BOOK
        t = rec(1001, "@GoLDVipSigbal", direction="BUY", tp2=4090.0, minutes_old=0)
        tm.trades = [t]
        _ORDER_BOOK = [t]
        tm.mt5.cancelled = []
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"fresh order NOT cancelled (status {t.status})",
               t.status == TradeStatus.PENDING.value and not tm.mt5.cancelled)

        t.timestamp = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"after the grace it IS cancelled (status {t.status})",
               t.status == TradeStatus.CANCELLED.value and 1001 in tm.mt5.cancelled)

        # ---------------- khan channel (TP1 rule) ----------------
        print("-- khan channel: cancel at TP1 --")
        k = rec(2001, "@forexkhan", direction="BUY", tp=4080.0, tp2=0.0, minutes_old=0)
        tm.trades = [k]
        _ORDER_BOOK = [k]
        tm.mt5.cancelled = []
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"fresh khan order NOT cancelled (status {k.status})",
               k.status == TradeStatus.PENDING.value and not tm.mt5.cancelled)

        k.timestamp = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"old khan order IS cancelled (status {k.status})",
               k.status == TradeStatus.CANCELLED.value)

        # ---------------- Brian dual entry (TP2 rule + partner) ----------------
        print("-- Brian dual entry: both legs --")
        b1 = rec(3001, "@BrianTradingForex", direction="BUY", entry=4000.0,
                 tp=4010.0, tp2=4090.0, minutes_old=0)
        b2 = rec(3002, "@BrianTradingForex", direction="BUY", entry=3995.0,
                 tp=4040.0, tp2=4090.0, minutes_old=0)
        tm.trades = [b1, b2]
        _ORDER_BOOK = [b1, b2]
        tm._linked_orders = {3001: {"partner_ticket": 3002, "breakeven_price": 3995.0}}
        tm.mt5.cancelled = []
        asyncio.run(tm.check_trade_updates())
        _check(failures, "fresh dual entry NOT cancelled (the reported bug)",
               b1.status == TradeStatus.PENDING.value
               and b2.status == TradeStatus.PENDING.value
               and not tm.mt5.cancelled)

        for r in (b1, b2):
            r.timestamp = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"old dual entry IS cancelled: leg1={b1.status}, leg2={b2.status}",
               b1.status == TradeStatus.CANCELLED.value
               and b2.status == TradeStatus.CANCELLED.value)
        _check(failures, "both legs were cancelled at the broker",
               3001 in tm.mt5.cancelled and 3002 in tm.mt5.cancelled)

        # ---------------- unreadable timestamp = never instant-cancel ----------------
        print("-- bad timestamp is treated as young --")
        bad = rec(4001, "@BrianTradingForex", direction="BUY", tp2=4090.0)
        bad.timestamp = "not-a-date"
        tm.trades = [bad]
        _ORDER_BOOK = [bad]
        tm.mt5.cancelled = []
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"unparsable timestamp -> no cancel (status {bad.status})",
               bad.status == TradeStatus.PENDING.value and not tm.mt5.cancelled)

        # ---------------- a SELL is handled on the ask side ----------------
        print("-- SELL direction --")
        s1 = rec(5001, "@BrianTradingForex", direction="SELL", entry=4100.0,
                 tp=4090.0, sl=4110.0, tp2=4000.0, minutes_old=20)
        tm.trades = [s1]
        _ORDER_BOOK = [s1]
        tm.mt5.cancelled = []
        tm.mt5.bid, tm.mt5.ask = 3995.0, 3996.0      # ask 3996 <= tp2 4000
        asyncio.run(tm.check_trade_updates())
        _check(failures, f"old SELL cancelled at TP2 (status {s1.status})",
               s1.status == TradeStatus.CANCELLED.value)
    finally:
        os.chdir(prev)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== pending-cancel grace period test ===")
    main()
