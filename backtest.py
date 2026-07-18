"""
Backtest module — fetches historical signals from a channel and checks
against MT5 historical price data to determine TP/SL outcomes.
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
    winrate: float = 0.0
    results: list = field(default_factory=list)  # List of per-signal results


class Backtester:
    """Backtests historical signals against MT5 price data."""

    def __init__(
        self,
        user_client,
        mt5_connector,
        logger: Optional[logging.Logger] = None,
    ):
        self.user_client = user_client  # Telethon client
        self.mt5 = mt5_connector
        self.logger = logger or logging.getLogger("xau_trader")

    async def fetch_channel_messages(
        self,
        channel_id: str,
        limit: int = 100,
    ) -> list:
        """Fetch last N messages from a channel via Telethon."""
        try:
            entity = await self.user_client.get_entity(channel_id)
            messages = []

            async for msg in self.user_client.iter_messages(entity, limit=limit):
                if msg.text:
                    messages.append({
                        "id": msg.id,
                        "text": msg.text,
                        "date": msg.date,
                    })

            self.logger.info(f"Fetched {len(messages)} messages from {channel_id}")
            return messages
        except Exception as e:
            self.logger.error(f"Failed to fetch messages from {channel_id}: {e}")
            return []

    def check_signal_outcome(
        self,
        signal: Signal,
        msg_date: datetime,
        tp_index: int = 2,
        hours_window: int = 8,
    ) -> dict:
        """
        Check what happened with a signal using MT5 historical data.

        Returns dict with:
        - status: "tp_hit", "sl_hit", "no_entry", "entry_hit_then_tp", "entry_hit_then_sl"
        - entry_price, tp_price, sl_price
        - entry_time (if hit)
        - close_time (if closed)
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

        # Fetch historical M1 data from signal time to +hours_window
        # MT5 copy_rates_from expects a timestamp
        from_date = msg_date.replace(tzinfo=None)
        # Add small buffer before signal
        from_date_buffer = from_date - timedelta(minutes=1)
        count = hours_window * 60 + 120  # minutes + buffer

        rates = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M1, from_date_buffer, count)

        if rates is None or len(rates) == 0:
            self.logger.error(f"No historical data for {symbol} from {from_date}")
            return {"status": "error", "message": "No historical data"}

        # Check if entry was hit and what happened after
        entry_filled = False
        entry_filled_time = None
        result_status = "no_entry"
        close_time = None

        for i, bar in enumerate(rates):
            bar_time = datetime.fromtimestamp(bar["time"])

            # Skip bars before signal time
            if bar_time < from_date:
                continue

            bar_high = bar["high"]
            bar_low = bar["low"]

            if not entry_filled:
                # Check if entry was hit
                if direction == "SELL":
                    # SELL LIMIT: price must rise to entry
                    if bar_high >= entry_price:
                        entry_filled = True
                        entry_filled_time = bar_time
                elif direction == "BUY":
                    # BUY LIMIT: price must fall to entry
                    if bar_low <= entry_price:
                        entry_filled = True
                        entry_filled_time = bar_time
            else:
                # Entry was filled, check TP and SL
                if direction == "SELL":
                    # For SELL: TP is below entry, SL is above
                    # Check SL first (worst case in same bar)
                    if bar_high >= sl_price:
                        result_status = "sl_hit"
                        close_time = bar_time
                        break
                    if bar_low <= tp_price:
                        result_status = "tp_hit"
                        close_time = bar_time
                        break
                elif direction == "BUY":
                    # For BUY: TP is above entry, SL is below
                    # Check SL first (worst case in same bar)
                    if bar_low <= sl_price:
                        result_status = "sl_hit"
                        close_time = bar_time
                        break
                    if bar_high >= tp_price:
                        result_status = "tp_hit"
                        close_time = bar_time
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
            "signal_time": msg_date.strftime("%Y-%m-%d %H:%M"),
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
        for msg in messages:
            text = msg["text"]
            msg_date = msg["date"]

            # Parse signal
            signal = parse_signal(text, channel_id, fmt)
            if not signal:
                continue

            # Also try auto if specified format failed
            if not signal:
                signal = parse_signal(text, channel_id, "auto")

            if not signal:
                continue

            result.parsed += 1

            # Check outcome
            outcome = self.check_signal_outcome(signal, msg_date, tp_index)

            if outcome["status"] == "error":
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

        # Calculate winrate
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
            f"  TP hit: {result.tp_hit}",
            f"  SL hit: {result.sl_hit}",
            f"Entry not filled: {result.no_entry}",
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
                    detail = "Entry not filled"
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
