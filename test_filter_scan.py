"""
Test: the backtest threshold scan (📐).

The scan re-scores the SAME simulated trades against candidate filter
thresholds, so you can pick a stricter setting that actually separates losers
from winners instead of just blocking more trades.

Verifies:
  - it finds the threshold that removes the losing trades (positive delta)
  - it reports the combined effect and says HELPS
  - it does NOT invent a benefit when every threshold would hurt
  - it stays silent on tiny samples (< SCAN_MIN_TRADES)
  - disabled filters are skipped

Pure unit test — builds TradeOutcome objects directly, no MT5, no network.
Run:  python test_filter_scan.py
"""

import sys
import os
import json
import tempfile

sys.modules.setdefault("MetaTrader5", type(sys)("MetaTrader5"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                       # noqa: E402
from backtest import Backtester, BacktestResult, TradeOutcome   # noqa: E402


def make_settings(tmpdir, validation=None):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": []},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": [{"id": "@TestCh", "format": "auto"}],
        "trading": {"lot_size": 0.01, "bot_active": True},
        "validation": validation or {
            "mode": "dry_run",
            "ema200": {"enabled": True, "buffer_atr_mult": 0.3},
            "rsi": {"enabled": True, "buy_max": 75, "sell_min": 25},
            "atr_sl": {"enabled": True, "min_sl_atr_mult": 0.5},
        },
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def outcome(status, profit, ema_atr=None, rsi=None, sl_atr=None):
    return TradeOutcome(
        direction="BUY", entry=4000.0, tp=4010.0, sl=3990.0, status=status,
        profit_pips=profit, m_ema_atr=ema_atr, m_rsi=rsi, m_sl_atr=sl_atr,
    )


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []
    tmpdir = tempfile.mkdtemp(prefix="xauscan_")
    B = Backtester(user_client=None, mt5_connector=None,
                   settings=make_settings(tmpdir))

    # ---- 1) losers are separable: counter-trend + exhausted entries ----
    print("-- scan finds the helpful thresholds --")
    res = BacktestResult(channel="@TestCh")
    # 6 winners, all trend-aligned, RSI mid
    for i in range(6):
        res.results.append(outcome("tp_hit", 90.0, ema_atr=1.5, rsi=55.0, sl_atr=1.0))
    # 4 losers that are counter-trend (far below the EMA)
    for i in range(4):
        res.results.append(outcome("sl_hit", -100.0, ema_atr=-2.0, rsi=55.0, sl_atr=1.0))
    # 2 losers entered on RSI exhaustion
    for i in range(2):
        res.results.append(outcome("sl_hit", -100.0, ema_atr=1.5, rsi=78.0, sl_atr=1.0))

    base_net = 6 * 90 - 6 * 100          # -60 pips
    rows = B._scan_table(res)
    text = "\n".join(rows)

    _check(failures, "scan produces output", bool(rows) and "Threshold scan" in text)
    _check(failures, "marks the current setting", "←current" in text)
    _check(failures, "finds a helping EMA200 threshold (✅)", "✅" in text)
    _check(failures, "reports the combined best", "🏆 Best combo" in text)
    _check(failures, "combo verdict says HELPS", "HELPS" in text)
    # blocking all 6 losers -> net 540; delta = 540 - (-60) = +600
    _check(failures, f"combo delta is +600 pips (text: {text.splitlines()[-2][-60:] if len(text.splitlines()) > 1 else ''})",
           "+600" in text)
    _check(failures, f"combo blocks all 6 losers ({base_net:+.0f} -> +540)",
           "+540" in text)

    # ---- 2) all winners: no threshold may claim a benefit ----
    print("-- no false benefit when every threshold hurts --")
    res2 = BacktestResult(channel="@TestCh")
    for i in range(12):
        res2.results.append(outcome("tp_hit", 90.0, ema_atr=1.5, rsi=55.0, sl_atr=1.0))
    rows2 = B._scan_table(res2)
    text2 = "\n".join(rows2)
    _check(failures, "no combo claimed", "Best combo" not in text2)
    _check(failures, "no ✅ verdict", "✅" not in text2)
    _check(failures, "stays silent when nothing can help", rows2 == [])

    # ---- 3) tiny sample: stay silent ----
    print("-- tiny samples stay silent --")
    res3 = BacktestResult(channel="@TestCh")
    for i in range(3):
        res3.results.append(outcome("sl_hit", -100.0, ema_atr=-2.0))
    _check(failures, "fewer than SCAN_MIN_TRADES -> no scan", B._scan_table(res3) == [])

    # ---- 4) disabled filter is skipped ----
    print("-- disabled filters are skipped --")
    B2 = Backtester(user_client=None, mt5_connector=None, settings=make_settings(
        tmpdir, validation={"mode": "dry_run",
                            "ema200": {"enabled": False},
                            "rsi": {"enabled": False},
                            "atr_sl": {"enabled": False}}))
    _check(failures, "all filters off -> no scan", B2._scan_table(res) == [])

    # ---- 5) rejected / no-data outcomes never enter the scan ----
    print("-- only closed trades are scored --")
    res4 = BacktestResult(channel="@TestCh")
    for i in range(6):
        res4.results.append(outcome("tp_hit", 90.0, ema_atr=1.5))
    for i in range(6):
        res4.results.append(outcome("sl_hit", -100.0, ema_atr=-2.0))
    res4.results.append(outcome("rejected", 0.0, ema_atr=-5.0))
    res4.results.append(outcome("not_filled", 0.0, ema_atr=-5.0))
    rows4 = "\n".join(B._scan_table(res4))
    _check(failures, "scan still works with mixed statuses", "Threshold scan" in rows4)

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== backtest threshold scan test ===")
    main()
