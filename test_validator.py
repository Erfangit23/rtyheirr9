"""
Test: signal quality filters (EMA200 regime / RSI exhaustion / ATR SL floor).

Verifies:
- indicator math sanity (EMA, Wilder RSI, Wilder ATR on known series)
- EMA200 check: pass/fail by regime side, neutral-buffer leniency
- RSI check: rejects only true exhaustion extremes
- ATR SL floor: rejects only noise-band stops
- fail-open when market data is unavailable
- per-channel overrides ("filters": false and per-filter overrides)
- end-to-end through TradeManager.process_signal in enforce and dry-run modes
- Telegram commands (/filters, /filtermode, /fema ...) via CommandHandler

Pure mocks — no MT5 connection, no Telegram, no network.
Run:  python test_validator.py
"""

import sys
import os
import json
import asyncio
import tempfile
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Fake MetaTrader5 module (imported by signal_validator and mt5_connector)
# ---------------------------------------------------------------------------
class FakeTick:
    def __init__(self, bid, ask, time=0):
        self.bid = bid
        self.ask = ask
        self.time = time


class FakeMT5Module:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(self):
        self.rates = {}   # tf constant -> list of bar dicts
        self.tick = FakeTick(4400.0, 4400.2)

    def copy_rates_from_pos(self, symbol, tf, start, count):
        bars = self.rates.get(tf)
        if bars is None:
            return None
        return bars[-count:]

    def symbol_info_tick(self, symbol):
        return self.tick


FAKE = FakeMT5Module()
sys.modules["MetaTrader5"] = FAKE

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from settings import Settings                  # noqa: E402
from signal_parser import Signal               # noqa: E402
from signal_validator import (                 # noqa: E402
    SignalValidator, ema_value, rsi_wilder, atr_wilder,
)
from trade_manager import TradeManager         # noqa: E402


def mk_bars(closes, step_seconds=3600):
    """Bar dicts from a close list; high=close+0.2, low=close-0.2."""
    t0 = 1_700_000_000
    return [
        {"time": t0 + i * step_seconds, "high": c + 0.2, "low": c - 0.2, "close": c}
        for i, c in enumerate(closes)
    ]


RISING = [4200.0 + i for i in range(400)]              # strong uptrend
FALLING = [4600.0 - i for i in range(400)]             # strong downtrend
FLAT_ALT = [4399.8, 4400.2] * 200                      # sideways, ATR ~0.6
M15_RISING = [4400.0 + i * 0.5 for i in range(300)]    # RSI ~100
M15_FALLING = [4400.0 - i * 0.5 for i in range(300)]   # RSI ~0
M15_ALT = [4400.2, 4399.8] * 150                       # RSI ~50, ATR ~0.6


def make_settings(tmpdir, validation=None, channels=None):
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "x", "phone": "x", "session_name": "s"},
        "report_bot": {"bot_token": "x", "authorized_user_ids": [111]},
        "mt5": {"login": 1, "password": "x", "server": "x",
                "terminal_path": "", "symbol": "XAUUSD"},
        "channels": channels or [{"id": "@TestCh", "format": "auto"}],
        "trading": {"lot_size": 0.01, "default_tp_index": 2, "max_sl_pips": 150,
                    "max_daily_sl_pips": 500, "bot_active": True,
                    "settings_password": "x", "mode_248": False},
    }
    if validation is not None:
        cfg["validation"] = validation
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return Settings(path)


def sig(direction="BUY", entry=4400.0, sl=4395.0, tps=None, channel="@TestCh"):
    return Signal(symbol="XAUUSD", direction=direction, entry=entry, stop_loss=sl,
                  take_profits=tps or [4410.0], source_channel=channel)


def _check(failures, label, cond):
    if cond:
        print(f"  PASS: {label}")
    else:
        print(f"  FAIL: {label}")
        failures.append(label)


def test_indicators():
    print("-- indicator math --")
    failures = []
    closes = [float(i) for i in range(201)]  # exactly one EMA step after the seed
    e = ema_value(closes, 200)
    k = 2.0 / 201
    expected = (sum(closes[:200]) / 200) * (1 - k) + 200.0 * k
    _check(failures, f"EMA one step after SMA seed (got {e:.4f}, want {expected:.4f})",
           abs(e - expected) < 1e-9)
    _check(failures, "EMA needs period+1 bars", ema_value(closes[:200], 200) is None)

    up = [4400.0 + i * 0.5 for i in range(100)]
    down = [4400.0 - i * 0.5 for i in range(100)]
    flat = [4400.2, 4399.8] * 60
    _check(failures, f"RSI all-gains ~100 (got {rsi_wilder(up, 14):.1f})",
           rsi_wilder(up, 14) > 99.0)
    _check(failures, f"RSI all-losses ~0 (got {rsi_wilder(down, 14):.1f})",
           rsi_wilder(down, 14) < 1.0)
    _check(failures, f"RSI alternating ~50 (got {rsi_wilder(flat, 14):.1f})",
           45.0 < rsi_wilder(flat, 14) < 55.0)

    highs = [c + 0.2 for c in flat]
    lows = [c - 0.2 for c in flat]
    a = atr_wilder(highs, lows, flat, 14)
    # TR for the alternation: max(0.4, |high - prev_close|=0.4, |low - prev_close|=0.6) = 0.6
    _check(failures, f"ATR alternation ~0.6 (got {a:.3f})", abs(a - 0.6) < 0.01)
    return failures


def test_ema200_check():
    print("-- EMA200 regime check --")
    failures = []
    tmp = tempfile.mkdtemp(prefix="xauvf_")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        s = make_settings(tmp, validation={"mode": "on"})
        v = SignalValidator(settings=s)

        # Uptrend: BUY passes, SELL fails (beyond buffer)
        FAKE.rates = {FAKE.TIMEFRAME_H1: mk_bars(RISING)}
        FAKE.tick = FakeTick(4599.0, 4599.2)
        c = v._check_ema200(s.filter_config_for_channel("@TestCh", "ema200"), sig("BUY"))
        _check(failures, f"uptrend BUY passes (got {c.detail})", c.passed)
        c = v._check_ema200(s.filter_config_for_channel("@TestCh", "ema200"), sig("SELL"))
        _check(failures, f"uptrend SELL fails (got {c.detail})", not c.passed)

        # Downtrend: SELL passes, BUY fails
        FAKE.rates = {FAKE.TIMEFRAME_H1: mk_bars(FALLING)}
        FAKE.tick = FakeTick(4201.0, 4201.2)
        c = v._check_ema200(s.filter_config_for_channel("@TestCh", "ema200"), sig("SELL"))
        _check(failures, f"downtrend SELL passes (got {c.detail})", c.passed)
        c = v._check_ema200(s.filter_config_for_channel("@TestCh", "ema200"), sig("BUY"))
        _check(failures, f"downtrend BUY fails (got {c.detail})", not c.passed)

        # Neutral buffer: price hugs EMA from the wrong side -> still passes
        FAKE.rates = {FAKE.TIMEFRAME_H1: mk_bars(FLAT_ALT)}
        FAKE.tick = FakeTick(4399.9, 4400.05)  # mid 4399.975, EMA 4400, buffer ~0.18
        c = v._check_ema200(s.filter_config_for_channel("@TestCh", "ema200"), sig("BUY"))
        _check(failures, f"neutral buffer BUY passes (got {c.detail})", c.passed)

        # No data -> fail-open
        FAKE.rates = {}
        c = v._check_ema200(s.filter_config_for_channel("@TestCh", "ema200"), sig("BUY"))
        _check(failures, "no data -> fail-open pass", c.passed)
        return failures
    finally:
        os.chdir(prev)


def test_rsi_check():
    print("-- RSI exhaustion check --")
    failures = []
    tmp = tempfile.mkdtemp(prefix="xauvr_")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        s = make_settings(tmp, validation={"mode": "on"})
        v = SignalValidator(settings=s)
        cfg = s.filter_config_for_channel("@TestCh", "rsi")

        FAKE.rates = {FAKE.TIMEFRAME_M15: mk_bars(M15_RISING, step_seconds=900)}
        c = v._check_rsi(cfg, sig("BUY"))
        _check(failures, f"rising M15 rejects BUY (got {c.detail})", not c.passed)
        c = v._check_rsi(cfg, sig("SELL"))
        _check(failures, f"rising M15 allows SELL (got {c.detail})", c.passed)

        FAKE.rates = {FAKE.TIMEFRAME_M15: mk_bars(M15_FALLING, step_seconds=900)}
        c = v._check_rsi(cfg, sig("SELL"))
        _check(failures, f"falling M15 rejects SELL (got {c.detail})", not c.passed)
        c = v._check_rsi(cfg, sig("BUY"))
        _check(failures, f"falling M15 allows BUY (got {c.detail})", c.passed)

        FAKE.rates = {FAKE.TIMEFRAME_M15: mk_bars(M15_ALT, step_seconds=900)}
        c = v._check_rsi(cfg, sig("BUY"))
        _check(failures, f"neutral M15 allows BUY (got {c.detail})", c.passed)

        FAKE.rates = {}
        c = v._check_rsi(cfg, sig("BUY"))
        _check(failures, "no data -> fail-open pass", c.passed)
        return failures
    finally:
        os.chdir(prev)


def test_atr_sl_check():
    print("-- ATR SL floor check --")
    failures = []
    tmp = tempfile.mkdtemp(prefix="xauva_")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        s = make_settings(tmp, validation={"mode": "on"})
        v = SignalValidator(settings=s)
        cfg = s.filter_config_for_channel("@TestCh", "atr_sl")

        FAKE.rates = {FAKE.TIMEFRAME_M15: mk_bars(M15_ALT, step_seconds=900)}  # ATR ~0.6 -> floor 0.3
        c = v._check_atr_sl(cfg, sig(entry=4400.0, sl=4399.95))   # 0.05 dist (0.5 pip)
        _check(failures, f"noise-band SL rejected (got {c.detail})", not c.passed)
        c = v._check_atr_sl(cfg, sig(entry=4400.0, sl=4395.0))    # 5.0 dist (50 pips)
        _check(failures, f"wide-enough SL passes (got {c.detail})", c.passed)

        FAKE.rates = {}
        c = v._check_atr_sl(cfg, sig(entry=4400.0, sl=4399.95))
        _check(failures, "no data -> fail-open pass", c.passed)
        return failures
    finally:
        os.chdir(prev)


def test_decision_and_overrides():
    print("-- decision + per-channel overrides --")
    failures = []
    tmp = tempfile.mkdtemp(prefix="xauvd_")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        chs = [
            {"id": "@TestCh", "format": "auto"},
            {"id": "@NoFilters", "format": "auto", "filters": False},
            {"id": "@NoRsi", "format": "auto", "filters": {"rsi": False}},
        ]
        s = make_settings(tmp, validation={"mode": "on"}, channels=chs)
        v = SignalValidator(settings=s)

        # Rising M15 => RSI rejects BUY; everything else passes
        FAKE.rates = {
            FAKE.TIMEFRAME_H1: mk_bars(RISING),
            FAKE.TIMEFRAME_M15: mk_bars(M15_RISING, step_seconds=900),
        }
        FAKE.tick = FakeTick(4599.0, 4599.2)

        d = v.validate(sig("BUY"))
        _check(failures, f"decision fails on RSI (got {[c.name for c in d.failures]})",
               not d.passed and len(d.failures) == 1 and d.failures[0].name.startswith("RSI"))
        _check(failures, f"mode carried ({d.mode})", d.mode == "on")

        # Channel with filters:false -> validate returns None
        _check(failures, "filters:false channel -> None",
               v.validate(sig("BUY", channel="@NoFilters")) is None)

        # Channel with rsi override off -> passes (only RSI was failing)
        d = v.validate(sig("BUY", channel="@NoRsi"))
        _check(failures, "rsi:false channel -> passes", d.passed)

        # Mode off -> None
        s.set_validation_mode("off")
        _check(failures, "mode off -> None", v.validate(sig("BUY")) is None)
        s.set_validation_mode("on")

        # Fail-open with no data at all
        FAKE.rates = {}
        d = v.validate(sig("BUY"))
        _check(failures, "no data anywhere -> decision passes", d.passed)
        return failures
    finally:
        os.chdir(prev)


# ---------------------------------------------------------------------------
# End-to-end: TradeManager hook + Telegram commands
# ---------------------------------------------------------------------------
class PlaceRecorder:
    def __init__(self):
        self.calls = 0

    def ensure_connected(self):
        return True

    def get_today_trade_summary(self):
        return {"deals": [], "total_loss_usd": 0.0}

    def get_symbol_price(self, symbol="XAUUSD"):
        return (4598.9, 4599.1)

    def place_limit_order(self, signal, lot_size, tp_index=2, max_sl_pips=150):
        self.calls += 1
        return 88001


def test_trade_manager_hook():
    print("-- TradeManager enforce/dry-run hook --")
    failures = []
    tmp = tempfile.mkdtemp(prefix="xauvh_")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        s = make_settings(tmp, validation={"mode": "on"})
        mt5rec = PlaceRecorder()
        tm = TradeManager(settings=s, mt5=mt5rec)
        tm._save_trades = lambda *a, **k: None
        tm._save_linked_orders = lambda *a, **k: None

        FAKE.rates = {
            FAKE.TIMEFRAME_H1: mk_bars(RISING),
            FAKE.TIMEFRAME_M15: mk_bars(M15_RISING, step_seconds=900),
        }
        FAKE.tick = FakeTick(4599.0, 4599.2)

        # Enforce: RSI fails -> order NOT placed
        asyncio.run(tm.process_signal(sig("BUY")))
        _check(failures, f"enforce blocks order (calls={mt5rec.calls})", mt5rec.calls == 0)

        # Dry-run: same signal -> order placed anyway
        s.set_validation_mode("dry_run")
        asyncio.run(tm.process_signal(sig("BUY")))
        _check(failures, f"dry-run still places order (calls={mt5rec.calls})",
               mt5rec.calls == 1)

        # Mode off: no filter activity at all
        s.set_validation_mode("off")
        asyncio.run(tm.process_signal(sig("BUY")))
        _check(failures, f"mode off places order (calls={mt5rec.calls})",
               mt5rec.calls == 2)
        return failures
    finally:
        os.chdir(prev)


def test_telegram_commands():
    print("-- Telegram filter commands --")
    failures = []
    tmp = tempfile.mkdtemp(prefix="xauvc_")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        chs = [{"id": "@TestCh", "format": "auto"},
               {"id": "@NoRsi", "format": "auto"}]
        s = make_settings(tmp, validation={"mode": "dry_run"}, channels=chs)
        from bot_commands import CommandHandler
        ch = CommandHandler(settings=s, mt5=None, trade_manager=None)

        out = asyncio.run(ch.handle("/filters", 111))
        _check(failures, "/filters shows DRY-RUN", "DRY-RUN" in out)
        _check(failures, "/filters lists all three filters",
               all(k in out for k in ("EMA200", "RSI", "ATR SL floor")))

        out = asyncio.run(ch.handle("/filtermode on", 111))
        _check(failures, "/filtermode on switches mode", s.get_validation_mode() == "on")
        _check(failures, "mode reply mentions ENFORCING", "ENFORCING" in out)

        out = asyncio.run(ch.handle("/frsi off", 111))
        _check(failures, "/frsi off disables globally",
               s.filter_config_for_channel("@TestCh", "rsi")["enabled"] is False)

        out = asyncio.run(ch.handle("/frsi on @NoRsi", 111))
        _check(failures, "/frsi on @NoRsi creates channel override",
               s.filter_config_for_channel("@NoRsi", "rsi")["enabled"] is True)
        _check(failures, "global rsi still off",
               s.filter_config_for_channel("@TestCh", "rsi")["enabled"] is False)

        out = asyncio.run(ch.handle("/fema off @TestCh", 111))
        cfg = s.filter_config_for_channel("@TestCh", "ema200")
        _check(failures, "/fema off @TestCh override",
               cfg is None or cfg.get("enabled") is False)

        out = asyncio.run(ch.handle("/fatr badinput", 111))
        _check(failures, "bad input shows usage", "Usage" in out)

        out = asyncio.run(ch.handle("/fema off @Missing", 111))
        _check(failures, "unknown channel rejected", "not found" in out)

        out = asyncio.run(ch.handle("/filters", 111))
        _check(failures, "/filters shows overrides section", "Per-channel overrides" in out)
        return failures
    finally:
        os.chdir(prev)


def main():
    all_fail = []
    for runner in (test_indicators, test_ema200_check, test_rsi_check,
                   test_atr_sl_check, test_decision_and_overrides,
                   test_trade_manager_hook, test_telegram_commands):
        all_fail += runner()
    print("=" * 40)
    if all_fail:
        print(f"RESULT: {len(all_fail)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== signal filter test ===")
    main()
