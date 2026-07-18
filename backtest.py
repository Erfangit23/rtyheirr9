"""
Backtest module — fetches historical signals from a channel and checks
against MT5 historical price data to determine TP/SL outcomes.

Key fixes:
- Timezone: Telegram returns UTC, MT5 uses broker local time.
  We convert UTC -> local time for MT5 calls.
- Entry window: Only check if entry was filled within 1 hour of signal.
  If not filled in 1 hour -> "no_entry" (excluded from winrate).
- After entry filled, check up to 8 hours for TP/SL.
"""

import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field

from signal_parser import parse_signal, Signal

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


@dataclass
class BacktestResult:
    total_signals: int = 0
    parsed: int = 0
    entry_hit: int = 0
    tp_hit: int = 0
    sl_hit: int = 0
    no_entry: int = 0
    errors: int = 0
    winrate: float = 0.0
    results: list = field(default_factory=list)


class Backtester:
    """Backtests historical signals against MT5 price data."""

    # Entry must be filled within this many minutes of signal
    ENTRY_WINDOW_MINUTES = 150

    # After entry, check this many hours for TP/SL
    TP_SL_WINDOW_HOURS = 8

    def __init__(
        self,
        user_client,
        mt5_connector,
        logger: Optional[logging.Logger] = None,
    ):
        self.user_client = user_client
        self.mt5 = mt5_connector
        self.logger = logger or logging.getLogger("xau_trader")

    async def fetch_channel_messages(
        self,
        channel_id: str,
        limit: int = 500,
        target_signals: int = 100,
    ) -> list:
        """Fetch messages from a channel until we have target_signals parsed signals.

        Fetches up to 'limit' messages but stops early once target_signals
        are collected.
        """
        try:
            entity = await self.user_client.get_entity(channel_id)
            messages = []
            signals_found = 0

            async for msg in self.user_client.iter_messages(entity, limit=limit):
                if msg.text:
                    messages.append({
                        "id": msg.id,
                        "text": msg.text,
                        "date": msg.date,  # UTC datetime
                    })
                    # Quick check if this looks like a signal
                    from signal_parser import parse_signal as _ps
                    if _ps(msg.text, channel_id, "auto"):
                        signals_found += 1
                        if signals_found >= target_signals:
                            break

            self.logger.info(
                f"Fetched {len(messages)} messages from {channel_id}, "
                f"{signals_found} signals found"
            )
            return messages
        except Exception as e:
            self.logger.error(f"Failed to fetch messages from {channel_id}: {e}")
            return []

    def _utc_to_local(self, utc_dt: datetime) -> datetime:
        """Convert UTC datetime to local time (for MT5 calls)."""
        if utc_dt.tzinfo is None:
            utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        # Convert to local time (VPS local time = broker time in most setups)
        local_dt = utc_dt.astimezone().replace(tzinfo=None)
        return local_dt

    def check_signal_outcome(
        self,
        signal: Signal,
        msg_date_utc: datetime,
        tp_index: int = 2,
    ) -> dict:
        """
        Check what happened with a signal using MT5 historical data.

        - Entry must be filled within 1 hour of signal time.
        - After entry, check up to 8 hours for TP/SL.

        Returns dict with status and details.
        """
        if not self.mt5.ensure_connected():
            return {"status": "error", "message": "MT5 not connected"}

        symbol = signal.symbol
        direction = signal.direction.upper()

        # Get TP at specified index
        if tp_index > len(signal.take_profits):
            tp_index = len(signal.take_profits)
        tp_price = signal.take_profits[tp_index - 1]
        sl_price = signal.stop_loss
        entry_price = signal.entry

        # Convert signal time from UTC to local (broker) time
        signal_local = self._utc_to_local(msg_date_utc)

        # Entry window: signal time to +1 hour
        entry_window_end = signal_local + timedelta(minutes=self.ENTRY_WINDOW_MINUTES)

        # Full window for data fetch: signal time to +9 hours (1h entry + 8h tp/sl)
        full_window_end = signal_local + timedelta(hours=self.TP_SL_WINDOW_HOURS + 1)

        # Fetch M1 data covering the full window
        # copy_rates_range takes (symbol, timeframe, from, to) as local datetimes
        rates = mt5.copy_rates_range(
            symbol,
            mt5.TIMEFRAME_M1,
            signal_local - timedelta(minutes=1),
            full_window_end + timedelta(minutes=1),
        )

        if rates is None or len(rates) == 0:
            self.logger.warning(
                f"No historical data for {symbol} around {signal_local}. "
                f"Trying copy_rates_from..."
            )
            # Fallback: copy_rates_from
            count = (self.TP_SL_WINDOW_HOURS + 2) * 60
            rates = mt5.copy_rates_from(
                symbol, mt5.TIMEFRAME_M1,
                signal_local - timedelta(minutes=1),
                count,
            )
            if rates is None or len(rates) == 0:
                self.logger.error(f"Still no data for {symbol} at {signal_local}")
                return {"status": "error", "message": "No historical data"}

        self.logger.info(
            f"Backtest: {direction} {symbol} entry={entry_price} "
            f"signal_local={signal_local.strftime('%Y-%m-%d %H:%M')} "
            f"rates={len(rates)} bars"
        )

        entry_filled = False
        entry_filled_time = None
        result_status = "no_entry"
        close_time = None

        for i, bar in enumerate(rates):
            # bar["time"] is epoch seconds in broker local time
            bar_time = datetime.fromtimestamp(bar["time"])

            # Skip bars before signal time
            if bar_time < signal_local:
                continue

            bar_high = bar["high"]
            bar_low = bar["low"]

            if not entry_filled:
                # Only check entry within 1 hour of signal
                if bar_time > entry_window_end:
                    # Entry window expired
                    result_status = "no_entry"
                    break

                # Check if entry was hit
                if direction == "SELL":
                    # SELL LIMIT: price must rise to entry
                    if bar_high >= entry_price:
                        entry_filled = True
                        entry_filled_time = bar_time
                        self.logger.info(
                            f"  Entry filled at {bar_time} "
                            f"(high={bar_high} >= {entry_price})"
                        )
                elif direction == "BUY":
                    # BUY LIMIT: price must fall to entry
                    if bar_low <= entry_price:
                        entry_filled = True
                        entry_filled_time = bar_time
                        self.logger.info(
                            f"  Entry filled at {bar_time} "
                            f"(low={bar_low} <= {entry_price})"
                        )
            else:
                # Entry was filled, check TP and SL
                # Use worst-case: check SL first in same bar
                if direction == "SELL":
                    if bar_high >= sl_price:
                        result_status = "sl_hit"
                        close_time = bar_time
                        self.logger.info(
                            f"  SL hit at {bar_time} "
                            f"(high={bar_high} >= SL={sl_price})"
                        )
                        break
                    if bar_low <= tp_price:
                        result_status = "tp_hit"
                        close_time = bar_time
                        self.logger.info(
                            f"  TP hit at {bar_time} "
                            f"(low={bar_low} <= TP={tp_price})"
                        )
                        break
                elif direction == "BUY":
                    if bar_low <= sl_price:
                        result_status = "sl_hit"
                        close_time = bar_time
                        self.logger.info(
                            f"  SL hit at {bar_time} "
                            f"(low={bar_low} <= SL={sl_price})"
                        )
                        break
                    if bar_high >= tp_price:
                        result_status = "tp_hit"
                        close_time = bar_time
                        self.logger.info(
                            f"  TP hit at {bar_time} "
                            f"(high={bar_high} >= TP={tp_price})"
                        )
                        break

        return {
            "status": result_status,
            "direction": direction,
            "entry": entry_price,
            "tp": tp_price,
            "sl": sl_price,
            "tp_index": tp_index,
            "entry_filled": entry_filled,
            "entry_time": entry_filled_time.strftime("%Y-%m-%d %H:%M") if entry_filled_time else None,
            "close_time": close_time.strftime("%Y-%m-%d %H:%M") if close_time else None,
            "signal_time": signal_local.strftime("%Y-%m-%d %H:%M"),
            "source": signal.source_channel,
        }

    async def run_backtest(
        self,
        channel_id: str,
        fmt: str = "auto",
        limit: int = 100,
        tp_index: int = 2,
    ) -> BacktestResult:
        """Run full backtest on a channel."""
        result = BacktestResult()

        # Fetch messages
        messages = await self.fetch_channel_messages(channel_id, limit)
        result.total_signals = len(messages)

        # Parse and test each message
        for idx, msg in enumerate(messages):
            text = msg["text"]
            msg_date = msg["date"]

            # Parse signal
            signal = parse_signal(text, channel_id, fmt)
            if not signal:
                signal = parse_signal(text, channel_id, "auto")

            if not signal:
                continue

            result.parsed += 1

            # Check outcome
            try:
                outcome = self.check_signal_outcome(signal, msg_date, tp_index)
            except Exception as e:
                self.logger.error(f"Backtest error on signal {idx}: {e}")
                result.errors += 1
                continue

            if outcome["status"] == "error":
                result.errors += 1
                continue

            if outcome["status"] == "no_entry":
                result.no_entry += 1
            elif outcome["status"] == "tp_hit":
                result.entry_hit += 1
                result.tp_hit += 1
            elif outcome["status"] == "sl_hit":
                result.entry_hit += 1
                result.sl_hit += 1

            result.results.append(outcome)

        # Calculate winrate (only closed trades: tp + sl)
        closed = result.tp_hit + result.sl_hit
        if closed > 0:
            result.winrate = (result.tp_hit / closed) * 100

        return result

    def format_results(self, result: BacktestResult, channel_id: str) -> str:
        """Format backtest results for Telegram message."""
        lines = [
            f"📊 Backtest Results: {channel_id}\n",
            f"Messages scanned: {result.total_signals}",
            f"Signals parsed: {result.parsed}",
            f"Entry filled: {result.entry_hit}",
            f"  ✅ TP hit: {result.tp_hit}",
            f"  ❌ SL hit: {result.sl_hit}",
            f"⚪ Entry not filled (1h timeout): {result.no_entry}",
            f"⚠️ Errors: {result.errors}",
            f"",
            f"🎯 Winrate: {result.winrate:.1f}% ({result.tp_hit}W / {result.sl_hit}L)",
            f"Closed trades: {result.tp_hit + result.sl_hit}",
        ]

        # Show last 10 detailed results
        if result.results:
            lines.append("\n--- Last 10 signals ---")
            recent = result.results[-10:]
            for r in recent:
                if r["status"] == "no_entry":
                    status_icon = "⚪"
                    detail = "Entry not filled (1h)"
                elif r["status"] == "tp_hit":
                    status_icon = "✅"
                    detail = f"TP hit at {r['close_time']}"
                elif r["status"] == "sl_hit":
                    status_icon = "❌"
                    detail = f"SL hit at {r['close_time']}"
                else:
                    status_icon = "❓"
                    detail = r["status"]

                lines.append(
                    f"{status_icon} {r['signal_time']} {r['direction']} "
                    f"Entry={r['entry']} -> {detail}"
                )

        return "\n".join(lines)
