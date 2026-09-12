"""
Signal quality filters — validate a parsed signal against live market
conditions before an order is placed.

Filters (each independently toggleable, per-channel overridable):
- ema200 : regime filter — BUY only above EMA200(H1), SELL only below.
           A neutral buffer (in ATR units) around the EMA prevents whipsaw
           rejections when price hugs the line. Optional over-extension
           guard (off by default).
- rsi    : exhaustion guard — reject BUY when RSI(14, M15) >= 75 and SELL
           when RSI <= 25. Wilder smoothing; wider than the classic 70/30
           because trending gold parks RSI beyond 70/30 for long stretches.
- atr_sl : noise floor — reject when the SL distance is inside the market's
           noise band (< 0.5 x ATR(14) on M15); such stops are coin flips.

Every check FAILS OPEN: if market data is unavailable or malformed the
check passes with a note, so a data glitch never silently stops trading.

Modes (config "validation.mode", controlled via Telegram /filtermode):
- "off"     filters skipped entirely
- "dry_run" failures are logged + reported but the trade is still placed
- "on"      failures block the signal

Indicators are computed in pure Python from MT5 broker candles, so values
match exactly what the terminal charts show. No external APIs.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


# ---------------------------------------------------------------------------
# Indicator math (pure Python, standard definitions)
# ---------------------------------------------------------------------------
def ema_value(closes, period: int) -> Optional[float]:
    """EMA seeded with the SMA of the first `period` closes (standard)."""
    if len(closes) < period + 1:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1.0 - k)
    return ema


def rsi_wilder(closes, period: int = 14) -> Optional[float]:
    """RSI with Wilder's smoothing (J. Welles Wilder, 1978)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr_wilder(highs, lows, closes, period: int = 14) -> Optional[float]:
    """Average True Range with Wilder's smoothing."""
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class FilterCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class ValidationDecision:
    mode: str = "dry_run"
    passed: bool = True
    checks: list = field(default_factory=list)

    @property
    def failures(self):
        return [c for c in self.checks if not c.passed]

    def failures_text(self) -> str:
        return "\n".join(f"• {c.name}: {c.detail}" for c in self.failures)


class SignalValidator:
    """Runs the configured quality filters on incoming signals."""

    def __init__(self, settings, logger: Optional[logging.Logger] = None):
        self.settings = settings
        self.logger = logger or logging.getLogger("xau_trader")

    # ------------------------------------------------------------------
    # Market data (fail-open)
    # ------------------------------------------------------------------
    def _get_bars(self, symbol: str, tf_name: str, count: int):
        """Completed bars (oldest->newest) as {highs, lows, closes}, or None."""
        try:
            if mt5 is None:
                return None
            tf = getattr(mt5, f"TIMEFRAME_{tf_name}", None)
            if tf is None:
                return None
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is None or len(rates) < 30:
                return None
            # Last row is the still-forming bar — drop it for stable values
            rates = rates[:-1]
            return {
                "highs": [float(r["high"]) for r in rates],
                "lows": [float(r["low"]) for r in rates],
                "closes": [float(r["close"]) for r in rates],
            }
        except Exception as e:
            self.logger.warning(f"Filter data fetch failed ({symbol} {tf_name}): {e}")
            return None

    def _mid_price(self, symbol: str):
        """(mid, tick) or (None, None)."""
        try:
            if mt5 is None:
                return None, None
            tick = mt5.symbol_info_tick(symbol)
            if tick is None or not tick.bid or not tick.ask:
                return None, None
            return (float(tick.bid) + float(tick.ask)) / 2.0, tick
        except Exception as e:
            self.logger.warning(f"Filter tick fetch failed ({symbol}): {e}")
            return None, None

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------
    def _check_ema200(self, cfg: dict, signal) -> FilterCheck:
        name = f"EMA200({cfg.get('timeframe', 'H1')})"
        tf = str(cfg.get("timeframe", "H1")).upper()
        bars = self._get_bars(signal.symbol, tf, 500)
        if bars is None or len(bars["closes"]) < 210:
            return FilterCheck(name, True, "fail-open (not enough chart history)")
        ema = ema_value(bars["closes"], 200)
        atr = atr_wilder(bars["highs"], bars["lows"], bars["closes"], 14)
        price, _ = self._mid_price(signal.symbol)
        if price is None:
            price = bars["closes"][-1]
        if ema is None:
            return FilterCheck(name, True, "fail-open (EMA not computable)")

        atr_val = atr if atr and atr > 0 else abs(price) * 0.001
        buffer = float(cfg.get("buffer_atr_mult", 0.3)) * atr_val
        diff = price - ema  # >0 => price above EMA
        direction = signal.direction.upper()

        if direction == "BUY":
            if diff < -buffer:
                return FilterCheck(
                    name, False,
                    f"price {price:.2f} is {abs(diff):.2f} BELOW EMA200 {ema:.2f} "
                    f"(downtrend; buffer {buffer:.2f})"
                )
            note = " (inside neutral buffer)" if diff < 0 else ""
            if cfg.get("ext_guard", False):
                ext = float(cfg.get("ext_guard_atr_mult", 2.5)) * atr_val
                if diff > ext:
                    return FilterCheck(
                        name, False,
                        f"price {price:.2f} is {diff:.2f} above EMA200 {ema:.2f} "
                        f"= over-extended (> {cfg.get('ext_guard_atr_mult', 2.5)}x ATR {ext:.2f}); chasing"
                    )
            return FilterCheck(name, True, f"price {price:.2f} above EMA200 {ema:.2f}{note}")
        else:  # SELL
            if diff > buffer:
                return FilterCheck(
                    name, False,
                    f"price {price:.2f} is {diff:.2f} ABOVE EMA200 {ema:.2f} "
                    f"(uptrend; buffer {buffer:.2f})"
                )
            note = " (inside neutral buffer)" if diff > 0 else ""
            if cfg.get("ext_guard", False):
                ext = float(cfg.get("ext_guard_atr_mult", 2.5)) * atr_val
                if -diff > ext:
                    return FilterCheck(
                        name, False,
                        f"price {price:.2f} is {abs(diff):.2f} below EMA200 {ema:.2f} "
                        f"= over-extended (> {cfg.get('ext_guard_atr_mult', 2.5)}x ATR {ext:.2f}); chasing"
                    )
            return FilterCheck(name, True, f"price {price:.2f} below EMA200 {ema:.2f}{note}")

    def _check_rsi(self, cfg: dict, signal) -> FilterCheck:
        period = int(cfg.get("period", 14))
        tf = str(cfg.get("timeframe", "M15")).upper()
        name = f"RSI({period}, {tf})"
        bars = self._get_bars(signal.symbol, tf, 300)
        if bars is None:
            return FilterCheck(name, True, "fail-open (no chart data)")
        rsi = rsi_wilder(bars["closes"], period)
        if rsi is None:
            return FilterCheck(name, True, "fail-open (RSI not computable)")
        buy_max = float(cfg.get("buy_max", 75))
        sell_min = float(cfg.get("sell_min", 25))
        direction = signal.direction.upper()
        if direction == "BUY" and rsi >= buy_max:
            return FilterCheck(name, False, f"RSI {rsi:.1f} >= {buy_max} (overbought exhaustion)")
        if direction == "SELL" and rsi <= sell_min:
            return FilterCheck(name, False, f"RSI {rsi:.1f} <= {sell_min} (oversold exhaustion)")
        return FilterCheck(name, True, f"RSI {rsi:.1f} within [{sell_min}, {buy_max}]")

    def _check_atr_sl(self, cfg: dict, signal) -> FilterCheck:
        period = int(cfg.get("period", 14))
        tf = str(cfg.get("timeframe", "M15")).upper()
        name = f"ATR SL floor({period}, {tf})"
        bars = self._get_bars(signal.symbol, tf, 100)
        if bars is None:
            return FilterCheck(name, True, "fail-open (no chart data)")
        atr = atr_wilder(bars["highs"], bars["lows"], bars["closes"], period)
        if not atr or atr <= 0:
            return FilterCheck(name, True, "fail-open (ATR not computable)")
        mult = float(cfg.get("min_sl_atr_mult", 0.5))
        floor = mult * atr
        dist = abs(float(signal.entry) - float(signal.stop_loss))
        if dist < floor:
            return FilterCheck(
                name, False,
                f"SL distance {dist:.2f} ({dist / 0.1:.0f} pips) inside noise floor "
                f"{floor:.2f} ({floor / 0.1:.0f} pips = {mult}x ATR)"
            )
        return FilterCheck(
            name, True,
            f"SL distance {dist / 0.1:.0f} pips >= floor {floor / 0.1:.0f} pips"
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def validate(self, signal) -> Optional[ValidationDecision]:
        """Validate a signal. Returns None when filtering is disabled for it,
        otherwise a ValidationDecision (passed=True means no filter objects)."""
        mode = self.settings.get_validation_mode()
        if mode == "off":
            return None
        if self.settings.channel_filters_disabled(signal.source_channel):
            return None

        checks = []
        for fname, runner in (
            ("ema200", self._check_ema200),
            ("rsi", self._check_rsi),
            ("atr_sl", self._check_atr_sl),
        ):
            cfg = self.settings.filter_config_for_channel(signal.source_channel, fname)
            if cfg and cfg.get("enabled", True):
                checks.append(runner(cfg, signal))

        decision = ValidationDecision(
            mode=mode,
            passed=all(c.passed for c in checks),
            checks=checks,
        )
        if decision.passed:
            self.logger.info(
                f"Filters: all checks passed for {signal.source_channel} "
                f"({len(checks)} checks)"
            )
        return decision
