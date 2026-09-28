"""
Test: backtest filter simulation (EMA200/RSI/ATR evaluated at signal time).

Verifies (synthetic H1/M15/M1 bars — no MT5, no network):
- A BUY signal in a DOWNTREND (price below EMA200) is marked blocked,
  with the failing filter named in filter_reasons.
- A BUY signal in an UPTREND-with-pullbacks (price above EMA200, RSI < 75)
  is NOT blocked.
- Aggregation: filter_blocked / blocked TP-SL split / blocked net pips /
  kept stats are all correct.
- format_results renders the 🧪 Filter simulation section and the verdict.
- Fail-open: missing H1/M15 history never counts as a block.

Run:  python test_backtest_filter.py
"""

import sys
import os
import json
import asyncio
import tempfile
import logging
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Fake MetaTrader5 module (only what the backtester touches)
# ---------------------------------------------------------------------------
class FakeMT5Module:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(self):
        self.by_tf = {self.TIMEFRAME_M1: [], self.TIMEFRAME_M15: [], self.TIMEFRAME_H1: []}
        self.h1_available = True
        self.m15_available = True

    def copy_rates_range(self, symbol, tf, frm, to):
        start = frm.timestamp()
        end = to.timestamp()
        if tf == self.TIMEFRAME_H1 and not self.h1_available:
            return None
        if tf == self.TIMEFRAME_M15 and not self.m15_available:
            return None
        return [r for r in self.by_tf.get(tf, []) if start <= r["time"] <= end]


FAKE = FakeMT5Module()
sys.modules["MetaTrader5"] = FAKE

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from backtest import Backtester                     # noqa: E402
from signal_parser import Signal                    # noqa: E402
from settings import Settings                       # noqa: E402


T0 = 1_700_000_000  # arbitrary epoch (broker time; offset mocked to 0)
H1, M15, M1 = 3600, 900, 60


def h1_bar(t, c):
    return {"time": t, "open": c, "high": c + 0.2, "low": c - 0.2, "close": c}


def build_history():
    """H1: 260 falling bars, then 320 rising-with-pullback bars."""
    h1, m15 = [], []
    # --- falling: closes -2 per bar (downtrend) ---
    fall = [4500.0 - 2 * i for i in range(260)]
    # --- rising: +5/-2 alternating (net uptrend, RSI ~71) ---
    rise, c = [], 3982.0
    for j in range(320):
        c += 5.0 if j % 2 == 0 else -2.0
        rise.append(c)

    for i, c in enumerate(fall):
        h1.append(h1_bar(T0 + i * H1, c))
    t_rise = T0 + 260 * H1
    for j, c in enumerate(rise):
        h1.append(h1_bar(t_rise + j * H1, c))

    # M15 mirrors both phases (falling before t1, rising after)
    for i, c in enumerate(fall[-60:]):           # ~15h of falling M15 before t1
        for k in range(4):
            m15.append(h1_bar(T0 + (200 + i) * H1 + k * M15, c - 0.5))
    for j, c in enumerate(rise):                 # full rising period in M15
        for k in range(4):
            m15.append(h1_bar(t_rise + j * H1 + k * M15, c))
    FAKE.by_tf[FAKE.TIMEFRAME_H1] = h1
    FAKE.by_tf[FAKE.TIMEFRAME_M15] = m15
    return t_rise  # t1 = end of downtrend / start of rise


T1 = build_history()
T2 = T1 + 320 * H1  # end of rising period


def m1_bar(t, high, low):
    return {"time": t, "open": (high + low) / 2, "high": high, "low": low,
            "close": (high + low) / 2}


FAKE.by_tf[FAKE.TIMEFRAME_M1] = [
    # signal 1 (BUY in downtrend): fills then SL
    m1_bar(T1 + 60, 4005, 3995),
    m1_bar(T1 + 120, 4000, 3985),
    # signal 2 (BUY in uptrend): fills then TP2 (default index 2 -> 4420)
    m1_bar(T2 + 60, 4405, 4395),
    m1_bar(T2 + 120, 4425, 4400),
]


def make_settings(tmpdir):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@testch", "format": "auto"}],
        "trading": {"lot_size": 0.01, "bot_active": True},
        # validation defaults (all filters ON, live thresholds)
        "validation": {
            "mode": "dry_run",
            "ema200": {"enabled": True, "timeframe": "H1", "buffer_atr_mult": 0.3},
            "rsi": {"enabled": True, "timeframe": "M15", "period": 14,
                    "buy_max": 75, "sell_min": 25},
            "atr_sl": {"enabled": True, "timeframe": "M15", "period": 14,
                       "min_sl_atr_mult": 0.5},
        },
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


class FakeConn:
    def ensure_connected(self):
        return True


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []
    logging.basicConfig(level=logging.CRITICAL)
    log = logging.getLogger("t")
    tmpdir = tempfile.mkdtemp(prefix="xaubtflt_")

    sig_down = Signal(symbol="XAUUSD", direction="BUY", entry=4000.0,
                      stop_loss=3990.0, take_profits=[4010.0, 4020.0],
                      source_channel="@testch")
    sig_up = Signal(symbol="XAUUSD", direction="BUY", entry=4400.0,
                    stop_loss=4390.0, take_profits=[4410.0, 4420.0],
                    source_channel="@testch")

    B = Backtester(user_client=None, mt5_connector=FakeConn(), logger=log,
                   settings=make_settings(tmpdir))
    B._server_offset = lambda symbol: 0.0

    async def fake_fetch(channel_id, target, fmt="auto"):
        return [
            {"signal": sig_down, "date": datetime.fromtimestamp(T1, tz=timezone.utc)},
            {"signal": sig_up, "date": datetime.fromtimestamp(T2, tz=timezone.utc)},
        ], 2

    B.fetch_signals = fake_fetch
    result = asyncio.run(B.run_backtest("@testch", fmt="auto"))

    print("-- filter simulation --")
    _check(failures, "simulation ran", result.filter_sim is True)
    _check(failures, f"exactly 1 blocked (got {result.filter_blocked})",
           result.filter_blocked == 1)
    _check(failures, f"blocked trade was the SL loser (tp={result.filter_blocked_tp}, sl={result.filter_blocked_sl})",
           result.filter_blocked_tp == 0 and result.filter_blocked_sl == 1)
    _check(failures, f"blocked net -100 pips (got {result.filter_blocked_net})",
           result.filter_blocked_net == -100.0)
    _check(failures, f"kept: 1 TP / 0 SL (got {result.kept_tp}/{result.kept_sl})",
           result.kept_tp == 1 and result.kept_sl == 0)
    _check(failures, f"kept net +200 pips (got {result.kept_net})",
           result.kept_net == 200.0)
    _check(failures, f"reason counts == ema200:1 (got {result.filter_reason_counts})",
           result.filter_reason_counts == {"ema200": 1})
    blocked_out = [o for o in result.results if o.filter_blocked]
    _check(failures, "blocked outcome carries reason text",
           blocked_out and "ema200" in blocked_out[0].filter_reasons)
    _check(failures, f"overall winrate 50% (got {result.winrate})",
           abs(result.winrate - 50.0) < 1e-9)

    print("-- report text --")
    text = B.format_results(result)
    _check(failures, "report has filter section", "Filter simulation" in text)
    _check(failures, "report verdict says filters HELP", "HELP" in text)
    _check(failures, "report shows winrate shift", "50.0% → 100.0%" in text)
    _check(failures, "last-15 marks the filtered trade", "🧪filtered" in text)

    print("-- fail-open (no chart data) --")
    FAKE.h1_available = False
    FAKE.m15_available = False
    B2 = Backtester(user_client=None, mt5_connector=FakeConn(), logger=log,
                    settings=make_settings(tmpdir))
    B2._server_offset = lambda symbol: 0.0
    B2.fetch_signals = fake_fetch
    result2 = asyncio.run(B2.run_backtest("@testch", fmt="auto"))
    _check(failures, "no data -> nothing blocked (fail-open)",
           result2.filter_sim is True and result2.filter_blocked == 0)
    FAKE.h1_available = True
    FAKE.m15_available = True

    print("-- no settings -> simulation disabled --")
    B3 = Backtester(user_client=None, mt5_connector=FakeConn(), logger=log)
    fails3, metrics3 = B3._filter_failures(sig_up, T2)
    _check(failures, "no settings: _filter_failures returns (None, {})",
           fails3 is None and metrics3 == {})

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== backtest filter simulation test ===")
    main()
