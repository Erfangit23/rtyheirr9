"""
Backtest module — fetches historical signals from a channel and checks
against MT5 historical price data to determine TP/SL outcomes.

Improvements:
- Scans up to 3000 messages to find more signals
- Entry window: 3 hours (180 min) — more realistic for limit orders
- After entry filled, check up to 12 hours for TP/SL
- Calculates risk-reward (RR) ratio for each trade
- Better "not filled" detection — checks if price came close to entry
- Detailed per-signal report with RR, entry, TP, SL
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
    avg_rr: float = 0.0
    total_profit_pips: float = 0.0
    total_loss_pips: float = 0.0
    net_pips: float = 0.0
    results: list = field(default_factory=list)


class Backtester:
    """Backtests historical signals against MT5 price data."""

    # Entry must be filled within this many minutes of signal
    ENTRY_WINDOW_MINUTES = 180  # 3 hours

    # After entry, check this many hours for TP/SL
    TP_SL_WINDOW_HOURS = 12

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
        limit: int = 3000,
        target_signals: int = 500,
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
                    # Quick pre-filter: must contain XAUUSD or XAU
                    text_upper = msg.text.upper()
                    if "XAUUSD" not in text_upper and "XAU" not in text_upper:
                        continue

                    messages.append({
                        "id": msg.id,
                        "text": msg.text,
                        "date": msg.date,  # UTC datetime
                    })

                    # Check if this is actually a parseable signal
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
        local_dt = utc_dt.astimezone().replace(tzinfo=None)
        return local_dt

    def _calculate_rr(self, entry: float, tp: float, sl: float) -> float:
        """Calculate risk-reward ratio."""
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return 0.0
        return reward / risk

    def _pips_between(self, price1: float, price2: float) -> float:
        """Calculate pips between two prices (gold: 1 pip = 0.1)."""
        return abs(price1 - price2) / 0.1

    def check_signal_outcome(
        self,
        signal: Signal,
        msg_date_utc: datetime,
        tp_index: int = 2,
    ) -> dict:
        """
        Check what happened with a signal using MT5 historical data.

        - Entry must be filled within ENTRY_WINDOW_MINUTES of signal time.
        - After entry, check up to TP_SL_WINDOW_HOURS for TP/SL.
        - Uses M1 (1-minute) bars for precise entry/exit detection.

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

        # Calculate RR
        rr = self._calculate_rr(entry_price, tp_price, sl_price)

        # Risk and reward in pips
        risk_pips = self._pips_between(entry_price, sl_price)
        reward_pips = self._pips_between(entry_price, tp_price)

        # Convert signal time from UTC to local (broker) time
        signal_local = self._utc_to_local(msg_date_utc)

        # Entry window
        entry_window_end = signal_local + timedelta(minutes=self.ENTRY_WINDOW_MINUTES)

        # Full window for data fetch
        full_window_end = signal_local + timedelta(hours=self.TP_SL_WINDOW_HOURS + 1)

        # Fetch M1 data covering the full window
        rates = mt5.copy_rates_range(
            symbol,
            mt5.TIMEFRAME_M1,
            signal_local - timedelta(minutes=5),
            full_window_end + timedelta(minutes=5),
        )

        if rates is None or len(rates) == 0:
            # Fallback: copy_rates_from with count
            count = (self.TP_SL_WINDOW_HOURS + 2) * 60
            rates = mt5.copy_rates_from(
                symbol, mt5.TIMEFRAME_M1,
                signal_local - timedelta(minutes=5),
                count,
            )
            if rates is None or len(rates) == 0:
                self.logger.error(f"No historical data for {symbol} at {signal_local}")
                return {"status": "error", "message": "No historical data"}

        self.logger.info(
            f"Backtest: {direction} {symbol} entry={entry_price} "
            f"signal_local={signal_local.strftime('%Y-%m-%d %H:%M')} "
            f"rates={len(rates)} bars | RR=1:{rr:.2f} "
            f"risk={risk_pips:.0f}pips reward={reward_pips:.0f}pips"
        )

        entry_filled = False
        entry_filled_time = None
        result_status = "no_entry"
        close_time = None
        min_distance_to_entry = float('inf')  # Track how close price came

        for i, bar in enumerate(rates):
            bar_time = datetime.fromtimestamp(bar["time"])

            # Skip bars before signal time
            if bar_time < signal_local:
                continue

            bar_high = bar["high"]
            bar_low = bar["low"]
            bar_open = bar["open"]
            bar_close = bar["close"]

            if not entry_filled:
                # Only check entry within window
                if bar_time > entry_window_end:
                    # Entry window expired — record how close we got
                    result_status = "no_entry"
                    break

                # Track closest distance to entry
                if direction == "SELL":
                    # SELL LIMIT: price must rise to entry
                    distance = bar_high - entry_price
                    if distance >= 0:
                        entry_filled = True
                        entry_filled_time = bar_time
                    else:
                        # Price didn't reach entry; track how close
                        min_distance_to_entry = min(min_distance_to_entry, abs(distance))
                elif direction == "BUY":
                    # BUY LIMIT: price must fall to entry
                    distance = entry_price - bar_low
                    if distance >= 0:
                        entry_filled = True
                        entry_filled_time = bar_time
                    else:
                        min_distance_to_entry = min(min_distance_to_entry, abs(distance))
            else:
                # Entry was filled, check TP and SL
                # Check both in same bar — use conservative approach:
                # If both TP and SL could have been hit in same bar,
                # assume SL first (worst case)
                if direction == "SELL":
                    if bar_high >= sl_price:
                        result_status = "sl_hit"
                        close_time = bar_time
                        break
                    if bar_low <= tp_price:
                        result_status = "tp_hit"
                        close_time = bar_time
                        break
                elif direction == "BUY":
                    if bar_low <= sl_price:
                        result_status = "sl_hit"
                        close_time = bar_time
                        break
                    if bar_high >= tp_price:
                        result_status = "tp_hit"
                        close_time = bar_time
                        break

        # Calculate pips for result
        profit_pips = 0.0
        if result_status == "tp_hit":
            profit_pips = reward_pips
        elif result_status == "sl_hit":
            profit_pips = -risk_pips

        # Convert closest distance to pips for no_entry
        closest_pips = 0.0
        if result_status == "no_entry" and min_distance_to_entry != float('inf'):
            closest_pips = min_distance_to_entry / 0.1

        return {
            "status": result_status,
            "direction": direction,
            "entry": entry_price,
            "tp": tp_price,
            "sl": sl_price,
            "tp_index": tp_index,
            "rr": rr,
            "risk_pips": risk_pips,
            "reward_pips": reward_pips,
            "profit_pips": profit_pips,
            "entry_filled": entry_filled,
            "entry_time": entry_filled_time.strftime("%Y-%m-%d %H:%M") if entry_filled_time else None,
            "close_time": close_time.strftime("%Y-%m-%d %H:%M") if close_time else None,
            "signal_time": signal_local.strftime("%Y-%m-%d %H:%M"),
            "source": signal.source_channel,
            "closest_pips": closest_pips,  # How close price came to entry (for no_entry)
        }

    async def run_backtest(
        self,
        channel_id: str,
        fmt: str = "auto",
        limit: int = 3000,
        target_signals: int = 500,
        tp_index: int = 2,
    ) -> BacktestResult:
        """Run full backtest on a channel."""
        result = BacktestResult()

        # Fetch messages
        messages = await self.fetch_channel_messages(channel_id, limit, target_signals)
        result.total_signals = len(messages)

        # Parse and test each message
        for idx, msg in enumerate(messages):
            text = msg["text"]
            msg_date = msg["date"]

            # Parse signal — try specified format first, then auto
            signal = parse_signal(text, channel_id, fmt)
            if not signal:
                signal = parse_signal(text, channel_id, "auto")

            if not signal:
                continue

            result.parsed += 1

            # Determine TP index for specific channels
            use_tp_index = tp_index
            if channel_id == "@gold_alicxzos110":
                use_tp_index = 4
            elif channel_id == "@forexkhan":
                use_tp_index = 1
            elif channel_id == "@Signal_Atlas":
                use_tp_index = 2

            # Check outcome
            try:
                outcome = self.check_signal_outcome(signal, msg_date, use_tp_index)
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
                result.total_profit_pips += outcome["profit_pips"]
            elif outcome["status"] == "sl_hit":
                result.entry_hit += 1
                result.sl_hit += 1
                result.total_loss_pips += abs(outcome["profit_pips"])

            result.results.append(outcome)

        # Calculate winrate (only closed trades: tp + sl)
        closed = result.tp_hit + result.sl_hit
        if closed > 0:
            result.winrate = (result.tp_hit / closed) * 100

        # Calculate net pips
        result.net_pips = result.total_profit_pips - result.total_loss_pips

        # Calculate average RR
        rr_values = [r["rr"] for r in result.results if r.get("rr", 0) > 0]
        if rr_values:
            result.avg_rr = sum(rr_values) / len(rr_values)

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
            f"⚪ Entry not filled: {result.no_entry}",
            f"⚠️ Errors: {result.errors}",
            f"",
            f"🎯 Winrate: {result.winrate:.1f}% ({result.tp_hit}W / {result.sl_hit}L)",
            f"📊 Avg RR: 1:{result.avg_rr:.2f}",
            f"💰 Net pips: {result.net_pips:+.0f} pips",
            f"   Profit: +{result.total_profit_pips:.0f} | Loss: -{result.total_loss_pips:.0f}",
            f"Closed trades: {result.tp_hit + result.sl_hit}",
        ]

        # Show last 15 detailed results
        if result.results:
            lines.append("\n--- Last 15 signals ---")
            recent = result.results[-15:]
            for r in recent:
                if r["status"] == "no_entry":
                    status_icon = "⚪"
                    detail = f"Not filled (closest: {r.get('closest_pips', 0):.0f} pips away)"
                elif r["status"] == "tp_hit":
                    status_icon = "✅"
                    detail = f"TP hit +{r['reward_pips']:.0f} pips at {r['close_time']}"
                elif r["status"] == "sl_hit":
                    status_icon = "❌"
                    detail = f"SL hit -{r['risk_pips']:.0f} pips at {r['close_time']}"
                else:
                    status_icon = "❓"
                    detail = r["status"]

                lines.append(
                    f"{status_icon} {r['signal_time']} {r['direction']} "
                    f"E={r['entry']} RR=1:{r.get('rr', 0):.1f} -> {detail}"
                )

        return "\n".join(lines)
