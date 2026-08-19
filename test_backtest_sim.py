"""
Test: backtest v2 simulator logic (synthetic M1 bars — no MT5, no network).

Verifies:
- single-leg fills, TP/SL outcomes, not-filled, expired
- Brian dual-entry: leg fills, TP1 -> breakeven on the farther leg, and the
  cancel-before-fill rule

Run:  python test_backtest_sim.py
"""

import sys
import os

sys.modules.setdefault("MetaTrader5", type(sys)("MetaTrader5"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from backtest import Backtester                       # noqa: E402
from signal_parser import Signal                      # noqa: E402


B = Backtester(user_client=None, mt5_connector=None)
T0 = 1_700_000_000  # arbitrary epoch


def bar(t, high, low):
    return {"time": t, "high": high, "low": low,
            "open": (high + low) / 2, "close": (high + low) / 2}


def _check(failures, label, cond):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        failures.append(label)


def main():
    failures = []

    # ---- single-leg: SELL TP hit ----
    rates = [bar(T0, 4395, 4390), bar(T0 + 60, 4405, 4398), bar(T0 + 120, 4402, 4389)]
    o = B._simulate_single("SELL", 4400, 4390, 4410, rates, T0)
    _check(failures, f"SELL tp_hit (got {o.status})", o.status == "tp_hit")
    _check(failures, f"SELL tp profit +100 (got {o.profit_pips})", o.profit_pips == 100.0)

    # ---- single-leg: SELL SL hit ----
    rates = [bar(T0, 4395, 4390), bar(T0 + 60, 4405, 4398), bar(T0 + 120, 4412, 4400)]
    o = B._simulate_single("SELL", 4400, 4390, 4410, rates, T0)
    _check(failures, f"SELL sl_hit (got {o.status})", o.status == "sl_hit")
    _check(failures, f"SELL sl profit -100 (got {o.profit_pips})", o.profit_pips == -100.0)

    # ---- single-leg: BUY TP hit ----
    rates = [bar(T0 + 60, 4398, 4395), bar(T0 + 120, 4412, 4399)]
    o = B._simulate_single("BUY", 4400, 4410, 4390, rates, T0)
    _check(failures, f"BUY tp_hit (got {o.status})", o.status == "tp_hit")
    _check(failures, f"BUY tp profit +100 (got {o.profit_pips})", o.profit_pips == 100.0)

    # ---- single-leg: not filled ----
    rates = [bar(T0 + 60, 4395, 4391), bar(T0 + 120, 4396, 4392)]
    o = B._simulate_single("SELL", 4400, 4390, 4410, rates, T0)
    _check(failures, f"not_filled (got {o.status})", o.status == "not_filled")

    # ---- single-leg: filled but expires after 24h ----
    rates = [bar(T0 + 60, 4405, 4398)]  # fill
    t = T0 + 120
    while t <= T0 + 26 * 3600:
        rates.append(bar(t, 4404, 4396))  # never touches 4390 or 4410
        t += 3600
    o = B._simulate_single("SELL", 4400, 4390, 4410, rates, T0)
    _check(failures, f"expired after window (got {o.status})", o.status == "expired")

    # ---- Brian dual: both fill, leg1 TP -> leg2 breakeven ----
    sig = Signal(symbol="XAUUSD", direction="BUY", entry=4063, stop_loss=4057,
                 take_profits=[4068, 4085], source_channel="@BrianTradingForex",
                 entries=[4060, 4063])
    rates = [
        bar(T0 + 60, 4062, 4059),   # fills both legs (low <= 4063 and <= 4060)
        bar(T0 + 120, 4070, 4064),  # leg1 TP (>= 4067) -> leg2 SL to 4060
        bar(T0 + 180, 4065, 4059),  # leg2 SL at breakeven 4060
    ]
    outs = B._simulate_brian(sig, rates, T0)
    _check(failures, f"two leg outcomes (got {len(outs)})", len(outs) == 2)
    if len(outs) == 2:
        l1, l2 = outs[0], outs[1]
        _check(failures, f"leg1 tp_hit (got {l1.status})", l1.status == "tp_hit")
        _check(failures, f"leg1 +40p (got {l1.profit_pips})", l1.profit_pips == 40.0)
        _check(failures, f"leg2 sl at breakeven (got {l2.status})", l2.status == "sl_hit")
        _check(failures, f"leg2 ~0p at breakeven (got {l2.profit_pips})", abs(l2.profit_pips) < 1e-9)

    # ---- Brian dual: TP1 before fill -> both cancelled ----
    rates = [bar(T0 + 60, 4068, 4064)]  # high >= tp1(4067), low never <= 4063
    outs = B._simulate_brian(sig, rates, T0)
    _check(failures, "both cancelled before fill",
           len(outs) == 2 and all(o.status == "cancelled" for o in outs))

    # ---- per-channel TP rules ----
    _check(failures, "gold_alicxzos110 uses TP4", B._channel_tp_index("@gold_alicxzos110", 5) == 4)
    _check(failures, "forexkhan uses TP1", B._channel_tp_index("@forexkhan", 3) == 1)
    _check(failures, "GoldVisionofficial uses TP3", B._channel_tp_index("@GoldVisionofficial", 4) == 3)
    _check(failures, "default TP2", B._channel_tp_index("@whatever", 3) == 2)

    # ---- Gulljanali adjustments ----
    gsig = Signal(symbol="XAUUSD", direction="BUY", entry=4400, stop_loss=4390,
                  take_profits=[4410, 4420], source_channel="@Gulljanali17")
    rates = [bar(T0, 4405, 4403)]  # market ~4404 -> entry adj valid
    B._apply_channel_adjustments(gsig, rates, T0)
    _check(failures, f"gulljanali entry 4400->4401 (got {gsig.entry})", gsig.entry == 4401.0)
    _check(failures, f"gulljanali SL 4390->4391 (got {gsig.stop_loss})", gsig.stop_loss == 4391.0)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== backtest v2 simulator test ===")
    main()
