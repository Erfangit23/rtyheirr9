"""
Test: backtest fidelity — the backtest must apply the SAME rules as live trading.

Two bugs seen in real /backtest output:

1. @Gulljanali17 reported "Net: +375720 pips" (≈ $37,572 on 0.01 lot) while its
   own trade list showed +90/+100 pips per trade. Cause: some messages contain
   numbers that are not prices (pip counts, dates). When one was captured as a
   TP/SL the levels became nonsense — and a BUY whose "TP" sits far BELOW the
   entry is touched on the very first bar, so it was booked as an instant win
   worth tens of thousands of pips. Live trading REJECTS such signals (wrong
   side of the entry); the backtest simulated them anyway.

2. @forexkhan showed 320/377 "not filled". Live cancels a khan pending order
   as soon as TP1 is reached without filling; the backtest didn't model that,
   so those orders were mislabelled.

Verifies:
  - _sanity_reject mirrors live (wrong-side SL/TP, SL cap, Brian bypass)
  - a wrong-side-TP signal is counted as rejected, never as a win
  - end-to-end: totals stay sane (no fantasy pips)
  - khan cancel rule: unfilled + TP1 reached -> cancelled (not "not filled")
  - khan regression: entry touched first -> normal TP hit

Run:  python test_backtest_fidelity.py
"""

import sys
import os
import asyncio
from datetime import datetime, timezone

# ---- fake MetaTrader5 (only what the backtester touches) ----
class FakeMT5Module:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385

    def __init__(self):
        self.m1 = []

    def copy_rates_range(self, symbol, tf, frm, to):
        if tf != self.TIMEFRAME_M1:
            return None
        start, end = frm.timestamp(), to.timestamp()
        return [r for r in self.m1 if start <= r["time"] <= end]


FAKE = FakeMT5Module()
sys.modules["MetaTrader5"] = FAKE

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from backtest import Backtester                       # noqa: E402
from signal_parser import Signal                      # noqa: E402


T0 = 1_700_000_000


def bar(t, high, low):
    return {"time": t, "open": (high + low) / 2, "high": high, "low": low,
            "close": (high + low) / 2}


class FakeConn:
    def ensure_connected(self):
        return True


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []
    B = Backtester(user_client=None, mt5_connector=FakeConn())

    # ---------------- 1) _sanity_reject mirrors live ----------------
    print("-- live-rule sanity check --")
    fantasy = Signal(symbol="XAUUSD", direction="BUY", entry=4200.0, stop_loss=4190.0,
                     take_profits=[100.0], source_channel="@Gulljanali17")
    _check(failures, "BUY with a far-below 'TP' is rejected",
           B._sanity_reject(fantasy) is not None)

    ok_buy = Signal(symbol="XAUUSD", direction="BUY", entry=4200.0, stop_loss=4190.0,
                    take_profits=[4210.0], source_channel="@Gulljanali17")
    _check(failures, "normal BUY passes", B._sanity_reject(ok_buy) is None)

    bad_sl_buy = Signal(symbol="XAUUSD", direction="BUY", entry=4200.0, stop_loss=4250.0,
                        take_profits=[4210.0], source_channel="@Gulljanali17")
    _check(failures, "BUY with SL above entry is rejected",
           B._sanity_reject(bad_sl_buy) is not None)

    bad_sl_sell = Signal(symbol="XAUUSD", direction="SELL", entry=4200.0, stop_loss=4150.0,
                         take_profits=[4190.0], source_channel="@Gulljanali17")
    _check(failures, "SELL with SL below entry is rejected",
           B._sanity_reject(bad_sl_sell) is not None)

    wide = Signal(symbol="XAUUSD", direction="SELL", entry=4200.0, stop_loss=4400.0,
                  take_profits=[4180.0], source_channel="@Gulljanali17")
    _check(failures, "SL wider than the 150-pip cap is rejected",
           B._sanity_reject(wide) is not None)
    _check(failures, "Brian dual entry is exempt from the SL cap",
           B._sanity_reject(wide, enforce_sl_cap=False) is None)

    # ---------------- 2) khan cancel rule ----------------
    print("-- khan cancel rule --")
    # SELL entry 4200 never touched (high stays < 4200); price falls to TP1 4180
    rates = [bar(T0 + 60, 4195, 4175)]
    o = B._simulate_single("SELL", 4200.0, 4180.0, 4210.0, rates, T0, cancel_at=4180.0)
    _check(failures, f"unfilled + TP1 reached -> cancelled (got {o.status})",
           o.status == "cancelled")

    # same bars without the khan rule -> plain "not filled"
    o2 = B._simulate_single("SELL", 4200.0, 4180.0, 4210.0, rates, T0)
    _check(failures, f"without the rule it is 'not_filled' (got {o2.status})",
           o2.status == "not_filled")

    # regression: entry touched first, then TP -> tp_hit
    rates2 = [bar(T0 + 60, 4205, 4199), bar(T0 + 120, 4201, 4179)]
    o3 = B._simulate_single("SELL", 4200.0, 4180.0, 4210.0, rates2, T0, cancel_at=4180.0)
    _check(failures, f"filled then TP -> tp_hit (got {o3.status})", o3.status == "tp_hit")

    # ---------------- 3) end-to-end: no fantasy pips ----------------
    print("-- end-to-end totals --")
    good = Signal(symbol="XAUUSD", direction="BUY", entry=4300.0, stop_loss=4290.0,
                  take_profits=[4310.0], source_channel="@Gulljanali17")
    FAKE.m1 = [
        bar(T0 + 60, 4305, 4295),    # good signal fills
        bar(T0 + 120, 4315, 4300),   # good signal TP
    ]
    B2 = Backtester(user_client=None, mt5_connector=FakeConn())
    B2._server_offset = lambda symbol: 0.0

    async def fake_fetch(channel_id, target, fmt="auto"):
        return [
            {"signal": fantasy, "date": datetime.fromtimestamp(T0, tz=timezone.utc)},
            {"signal": good, "date": datetime.fromtimestamp(T0, tz=timezone.utc)},
        ], 2

    B2.fetch_signals = fake_fetch
    res = asyncio.run(B2.run_backtest("@Gulljanali17", fmt="auto"))

    _check(failures, f"fantasy signal counted as rejected (got {res.rejected})",
           res.rejected == 1)
    _check(failures, f"gross profit sane, not 41,000 pips (got {res.gross_profit_pips:.0f})",
           res.gross_profit_pips == 100.0)
    _check(failures, f"only the real trade was simulated (trades={res.trades})",
           res.trades == 1)
    _check(failures, f"net = +100 pips (got {res.net_pips:.0f})", res.net_pips == 100.0)

    text = B2.format_results(res)
    _check(failures, "report shows the rejected count", "Rejected by live rules" in text)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== backtest fidelity test ===")
    main()
