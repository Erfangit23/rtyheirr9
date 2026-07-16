"""
Trade manager — processes signals, applies risk checks, and places orders.
Also tracks trades for reporting.
"""

import logging
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, asdict
from enum import Enum

from signal_parser import Signal
from settings import Settings
from mt5_connector import MT5Connector


class TradeStatus(str, Enum):
    PENDING = "pending"          # Limit order placed
    FILLED = "filled"            # Order became a position
    TP_HIT = "tp_hit"            # Take profit hit
    SL_HIT = "sl_hit"            # Stop loss hit
    CANCELLED = "cancelled"      # Order cancelled
    REJECTED_SL = "rejected_sl"  # Rejected due to SL limit
    REJECTED_DAILY = "rejected_daily"  # Rejected due to daily SL limit


@dataclass
class TradeRecord:
    ticket: int
    channel: str
    symbol: str
    direction: str
    entry: float
    sl: float
    tp: float
    tp_index: int
    lot_size: float
    status: str
    timestamp: str
    raw_signal: str = ""


class TradeManager:
    """Manages the full lifecycle of trades from signal to closure."""

    def __init__(
        self,
        settings: Settings,
        mt5: MT5Connector,
        report_callback=None,
        logger: Optional[logging.Logger] = None,
    ):
        self.settings = settings
        self.mt5 = mt5
        self.report_callback = report_callback  # async func(message: str)
        self.logger = logger or logging.getLogger("xau_trader")
        self.trades: list[TradeRecord] = []
        self.trades_file = "data/trades.json"
        self._load_trades()

    def _load_trades(self):
        """Load trade history from file."""
        os.makedirs("data", exist_ok=True)
        try:
            if os.path.exists(self.trades_file):
                with open(self.trades_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.trades = [TradeRecord(**t) for t in data]
                self.logger.info(f"Loaded {len(self.trades)} trade records.")
        except Exception as e:
            self.logger.error(f"Failed to load trades: {e}")
            self.trades = []

    def _save_trades(self):
        """Save trade history to file."""
        try:
            os.makedirs("data", exist_ok=True)
            with open(self.trades_file, "w", encoding="utf-8") as f:
                json.dump([asdict(t) for t in self.trades], f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error(f"Failed to save trades: {e}")

    async def process_signal(self, signal: Signal):
        """Process a parsed signal: apply risk checks and place order."""
        self.logger.info(f"Processing signal: {signal}")

        # Check if bot is active
        if not self.settings.bot_active:
            self.logger.info("Bot is sleeping (inactive). Signal ignored.")
            await self._report(
                f"💤 Signal received but bot is SLEEPING:\n"
                f"{signal.direction} {signal.symbol} Entry={signal.entry}\n"
                f"SL={signal.stop_loss} TPs={signal.take_profits}\n"
                f"Source: {signal.source_channel}"
            )
            return

        # Check daily SL limit
        daily_summary = self.mt5.get_today_trade_summary()
        daily_loss = daily_summary.get("total_loss_usd", 0.0)
        # We compare in USD terms as approximation
        # For more precise tracking, we'd convert pips to USD
        if daily_loss > 0:
            self.logger.info(f"Today's loss so far: {daily_loss:.2f} USD")

        # Get TP for the configured index
        tp_index = self.settings.default_tp_index
        if tp_index > len(signal.take_profits):
            tp_index = len(signal.take_profits)
            self.logger.warning(
                f"TP index {self.settings.default_tp_index} > available TPs "
                f"({len(signal.take_profits)}). Using TP{tp_index}."
            )

        # Place the limit order
        ticket = self.mt5.place_limit_order(
            signal=signal,
            lot_size=self.settings.lot_size,
            tp_index=tp_index,
            max_sl_pips=self.settings.max_sl_pips,
        )

        now = datetime.now(timezone.utc).isoformat()

        if ticket is None:
            self.logger.error("Order placement failed.")
            await self._report(
                f"❌ Order FAILED to place:\n"
                f"{signal.direction} {signal.symbol} Entry={signal.entry}\n"
                f"SL={signal.stop_loss} TP={signal.take_profits[tp_index-1]}\n"
                f"Source: {signal.source_channel}\n"
                f"Check MT5 connection and logs."
            )
            return

        if ticket == -1:
            # Rejected due to SL limit
            sl_pips = abs(signal.entry - signal.stop_loss) / 0.1
            record = TradeRecord(
                ticket=0,
                channel=signal.source_channel,
                symbol=signal.symbol,
                direction=signal.direction,
                entry=signal.entry,
                sl=signal.stop_loss,
                tp=signal.take_profits[tp_index - 1] if signal.take_profits else 0,
                tp_index=tp_index,
                lot_size=self.settings.lot_size,
                status=TradeStatus.REJECTED_SL.value,
                timestamp=now,
                raw_signal=signal.raw_text[:200],
            )
            self.trades.append(record)
            self._save_trades()
            await self._report(
                f"🚫 Order REJECTED - SL too large:\n"
                f"{signal.direction} {signal.symbol} Entry={signal.entry}\n"
                f"SL={signal.stop_loss} ({sl_pips:.0f} pips > {self.settings.max_sl_pips} max)\n"
                f"Source: {signal.source_channel}"
            )
            return

        # Success
        record = TradeRecord(
            ticket=ticket,
            channel=signal.source_channel,
            symbol=signal.symbol,
            direction=signal.direction,
            entry=signal.entry,
            sl=signal.stop_loss,
            tp=signal.take_profits[tp_index - 1] if signal.take_profits else 0,
            tp_index=tp_index,
            lot_size=self.settings.lot_size,
            status=TradeStatus.PENDING.value,
            timestamp=now,
            raw_signal=signal.raw_text[:200],
        )
        self.trades.append(record)
        self._save_trades()

        await self._report(
            f"✅ Limit order placed:\n"
            f"#{ticket} {signal.direction} {signal.symbol}\n"
            f"Entry: {signal.entry}\n"
            f"SL: {signal.stop_loss}\n"
            f"TP: {signal.take_profits[tp_index-1]} (TP{tp_index})\n"
            f"Lot: {self.settings.lot_size}\n"
            f"Source: {signal.source_channel}"
        )

    async def check_trade_updates(self):
        """Check for trade status updates (filled, TP hit, SL hit)."""
        if not self.mt5.ensure_connected():
            return

        # Get current positions and pending orders
        positions = self.mt5.get_open_positions()
        orders = self.mt5.get_pending_orders()

        position_tickets = {p.ticket for p in positions}
        order_tickets = {o.ticket for o in orders}

        now = datetime.now(timezone.utc).isoformat()

        # Check pending trades for status changes
        updated = False
        for trade in self.trades:
            if trade.status != TradeStatus.PENDING.value:
                continue

            if trade.ticket in position_tickets:
                # Order was filled
                trade.status = TradeStatus.FILLED.value
                updated = True
                await self._report(
                    f"🔄 Order FILLED:\n"
                    f"#{trade.ticket} {trade.direction} {trade.symbol}\n"
                    f"Entry: {trade.entry} | SL: {trade.sl} | TP: {trade.tp}\n"
                    f"Lot: {trade.lot_size}"
                )

            elif trade.ticket not in order_tickets and trade.ticket not in position_tickets:
                # Order is gone — could be TP hit, SL hit, or cancelled
                # Check deal history
                deal_info = self._check_deal_history(trade.ticket)
                if deal_info:
                    if deal_info["profit"] > 0:
                        trade.status = TradeStatus.TP_HIT.value
                        status_emoji = "🎯 TP HIT"
                    else:
                        trade.status = TradeStatus.SL_HIT.value
                        status_emoji = "🛑 SL HIT"

                    await self._report(
                        f"{status_emoji}:\n"
                        f"#{trade.ticket} {trade.direction} {trade.symbol}\n"
                        f"Entry: {trade.entry} | SL: {trade.sl} | TP: {trade.tp}\n"
                        f"Profit: {deal_info['profit']:.2f} USD\n"
                        f"Source: {trade.channel}"
                    )
                    updated = True
                else:
                    # Might have been cancelled manually
                    trade.status = TradeStatus.CANCELLED.value
                    await self._report(
                        f"❌ Order CANCELLED (not found):\n"
                        f"#{trade.ticket} {trade.direction} {trade.symbol}"
                    )
                    updated = True

            elif trade.ticket in order_tickets:
                # Order still pending — check if it's been 45 minutes
                try:
                    trade_time = datetime.fromisoformat(trade.timestamp)
                    elapsed = datetime.now(timezone.utc) - trade_time
                    if elapsed > timedelta(minutes=45):
                        self.logger.info(
                            f"Order #{trade.ticket} pending for {elapsed}, "
                            f"cancelling (45 min timeout)."
                        )
                        cancelled = self.mt5.cancel_order(trade.ticket)
                        if cancelled:
                            trade.status = TradeStatus.CANCELLED.value
                            updated = True
                            await self._report(
                                f"⏰ Order CANCELLED - 45 min timeout:\n"
                                f"#{trade.ticket} {trade.direction} {trade.symbol}\n"
                                f"Entry: {trade.entry} (never filled in 45 min)\n"
                                f"Source: {trade.channel}"
                            )
                        else:
                            self.logger.error(
                                f"Failed to cancel expired order #{trade.ticket}"
                            )
                except Exception as e:
                    self.logger.error(f"Timeout check error for trade #{trade.ticket}: {e}")

        if updated:
            self._save_trades()

    def get_channel_stats(self) -> dict:
        """Compute per-channel statistics from trade history."""
        stats = {}
        for trade in self.trades:
            ch = trade.channel
            if ch not in stats:
                stats[ch] = {
                    "total": 0,
                    "filled": 0,
                    "tp_hit": 0,
                    "sl_hit": 0,
                    "cancelled": 0,
                    "rejected": 0,
                    "pending": 0,
                    "profit": 0.0,
                }
            s = stats[ch]
            s["total"] += 1
            if trade.status == TradeStatus.PENDING.value:
                s["pending"] += 1
            elif trade.status == TradeStatus.FILLED.value:
                s["filled"] += 1
            elif trade.status == TradeStatus.TP_HIT.value:
                s["tp_hit"] += 1
            elif trade.status == TradeStatus.SL_HIT.value:
                s["sl_hit"] += 1
            elif trade.status == TradeStatus.CANCELLED.value:
                s["cancelled"] += 1
            elif trade.status == TradeStatus.REJECTED_SL.value:
                s["rejected"] += 1

        return stats

    def _check_deal_history(self, ticket: int) -> Optional[dict]:
        """Check deal history for a closed position by ticket."""
        try:
            from datetime import datetime, timezone, timedelta
            utc_to = datetime.now(timezone.utc)
            utc_from = utc_to - timedelta(hours=24)
            deals = self.mt5.ensure_connected() and __import__("MetaTrader5").history_deals_get(utc_from, utc_to)
            if deals:
                for deal in deals:
                    if deal.position_id == ticket or deal.order == ticket:
                        return {"profit": deal.profit, "price": deal.price}
        except Exception as e:
            self.logger.error(f"Deal history check error: {e}")
        return None

    async def _report(self, message: str):
        """Send a report via the callback."""
        if self.report_callback:
            try:
                await self.report_callback(message)
            except Exception as e:
                self.logger.error(f"Report callback error: {e}")
