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
    tp2: float = 0.0  # TP2 price for cancellation logic


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
        self._linked_orders = {}  # ticket -> {partner_ticket, breakeven_price}
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

        # Check blackout window: 13:00-15:00 Tehran time (GMT+3:30)
        # 13:00 Tehran = 09:30 UTC, 15:00 Tehran = 11:30 UTC
        from datetime import datetime, timezone, timedelta
        tehran_tz = timezone(timedelta(hours=3, minutes=30))
        now_tehran = datetime.now(tehran_tz)
        current_hour_min = now_tehran.hour * 100 + now_tehran.minute
        if 1300 <= current_hour_min < 1500:
            self.logger.info(
                f"Signal ignored - blackout window (13:00-15:00 Tehran). "
                f"Current Tehran time: {now_tehran.strftime('%H:%M')}"
            )
            await self._report(
                f"⏸️ Signal skipped - blackout window (13:00-15:00 Tehran):\n"
                f"{signal.direction} {signal.symbol} Entry={signal.entry}\n"
                f"Source: {signal.source_channel}\n"
                f"Current time: {now_tehran.strftime('%H:%M')} Tehran"
            )
            return

        # Check daily SL limit
        daily_summary = self.mt5.get_today_trade_summary()
        daily_loss = daily_summary.get("total_loss_usd", 0.0)
        # We compare in USD terms as approximation
        # For more precise tracking, we'd convert pips to USD
        if daily_loss > 0:
            self.logger.info(f"Today's loss so far: {daily_loss:.2f} USD")

        # Determine TP index and whether to place split orders
        # Per-channel TP override: gold_alicxzos110 uses TP3, others use default
        # BrianTradingForex: dual entry orders (entry1->TP1, entry2->TP2) with breakeven
        split_orders = False
        dual_entry = False
        if signal.source_channel == "@gold_alicxzos110":
            tp_index = 3
            self.logger.info("Channel @gold_alicxzos110: using TP3 override")
        elif signal.source_channel == "@BrianTradingForex":
            dual_entry = True
            tp_index = 2
            self.logger.info("Channel @BrianTradingForex: placing dual entry orders (entry1->TP1, entry2->TP2)")
        else:
            tp_index = self.settings.default_tp_index
        if tp_index > len(signal.take_profits):
            tp_index = len(signal.take_profits)
            self.logger.warning(
                f"TP index {tp_index} > available TPs "
                f"({len(signal.take_profits)}). Using TP{tp_index}."
            )

        if dual_entry:
            # Place two separate orders with different entries and TPs.
            # The closer-to-market entry gets TP1 (fills first).
            # The farther entry gets TP2 (fills later).
            # When the first order hits TP1, move the second order's SL to its entry (breakeven).
            if len(signal.entries) < 2 or len(signal.take_profits) < 2:
                self.logger.warning("Not enough entries/TPs for dual entry order, skipping.")
                return

            entry1, entry2 = signal.entries[0], signal.entries[1]
            tp1_price, tp2_price = signal.take_profits[0], signal.take_profits[1]

            # Determine which entry is closer to market (fills first)
            # For BUY: higher entry = closer to market (price drops to hit it)
            # For SELL: lower entry = closer to market (price rises to hit it)
            if signal.direction.upper() == "BUY":
                # Higher entry fills first -> TP1, lower entry fills later -> TP2
                if entry1 >= entry2:
                    first_entry, first_tp = entry1, tp1_price
                    second_entry, second_tp = entry2, tp2_price
                else:
                    first_entry, first_tp = entry2, tp1_price
                    second_entry, second_tp = entry1, tp2_price
            else:  # SELL
                # Lower entry fills first -> TP1, higher entry fills later -> TP2
                if entry1 <= entry2:
                    first_entry, first_tp = entry1, tp1_price
                    second_entry, second_tp = entry2, tp2_price
                else:
                    first_entry, first_tp = entry2, tp1_price
                    second_entry, second_tp = entry1, tp2_price

            self.logger.info(
                f"Dual entry: Order1 entry={first_entry} TP={first_tp} (closer, fills first), "
                f"Order2 entry={second_entry} TP={second_tp} (farther, breakeven on TP1)"
            )

            # Place both orders
            from signal_parser import Signal as Sig
            results = []
            for entry_val, tp_val, label in [
                (first_entry, first_tp, "first"),
                (second_entry, second_tp, "second"),
            ]:
                mod_signal = Sig(
                    symbol=signal.symbol,
                    direction=signal.direction,
                    entry=entry_val,
                    stop_loss=signal.stop_loss,
                    take_profits=[tp_val],  # Single TP
                    raw_text=signal.raw_text,
                    source_channel=signal.source_channel,
                )
                ticket = self.mt5.place_limit_order(
                    signal=mod_signal,
                    lot_size=self.settings.lot_size,
                    tp_index=1,  # Only 1 TP in the modified signal
                    max_sl_pips=self.settings.max_sl_pips,
                )
                results.append((label, entry_val, tp_val, ticket))

            now = datetime.now(timezone.utc).isoformat()

            # Check results
            all_failed = all(t is None for _, _, _, t in results)
            all_rejected = all(t == -1 for _, _, _, t in results)

            if all_failed:
                self.logger.error("Both dual entry orders failed to place.")
                await self._report(
                    f"❌ Both orders FAILED to place:\n"
                    f"{signal.direction} {signal.symbol}\n"
                    f"Source: {signal.source_channel}\n"
                    f"Check MT5 connection and logs."
                )
                return

            if all_rejected:
                sl_pips = abs(signal.entries[0] - signal.stop_loss) / 0.1
                for label, entry_val, tp_val, _ in results:
                    record = TradeRecord(
                        ticket=0,
                        channel=signal.source_channel,
                        symbol=signal.symbol,
                        direction=signal.direction,
                        entry=entry_val,
                        sl=signal.stop_loss,
                        tp=tp_val,
                        tp_index=1,
                        lot_size=self.settings.lot_size,
                        status=TradeStatus.REJECTED_SL.value,
                        timestamp=now,
                        raw_signal=signal.raw_text[:200],
                        tp2=signal.take_profits[1] if len(signal.take_profits) >= 2 else 0,
                    )
                    self.trades.append(record)
                self._save_trades()
                await self._report(
                    f"🚫 Both orders REJECTED - SL too large:\n"
                    f"{signal.direction} {signal.symbol}\n"
                    f"SL={signal.stop_loss} ({sl_pips:.0f} pips > {self.settings.max_sl_pips} max)\n"
                    f"Source: {signal.source_channel}"
                )
                return

            # Process results and link orders for breakeven tracking
            report_lines = [f"✅ Dual entry orders placed ({signal.source_channel}):"]
            tickets = []
            first_ticket = None
            second_ticket = None
            for label, entry_val, tp_val, ticket in results:
                tickets.append(ticket)
                if label == "first":
                    first_ticket = ticket if ticket and ticket > 0 else None
                else:
                    second_ticket = ticket if ticket and ticket > 0 else None

                if ticket is None:
                    report_lines.append(f"  [{label}] Entry @ {entry_val} TP @ {tp_val}: ❌ FAILED")
                    continue
                if ticket == -1:
                    report_lines.append(f"  [{label}] Entry @ {entry_val} TP @ {tp_val}: 🚫 REJECTED (SL too large)")
                    record = TradeRecord(
                        ticket=0,
                        channel=signal.source_channel,
                        symbol=signal.symbol,
                        direction=signal.direction,
                        entry=entry_val,
                        sl=signal.stop_loss,
                        tp=tp_val,
                        tp_index=1,
                        lot_size=self.settings.lot_size,
                        status=TradeStatus.REJECTED_SL.value,
                        timestamp=now,
                        raw_signal=signal.raw_text[:200],
                        tp2=signal.take_profits[1] if len(signal.take_profits) >= 2 else 0,
                    )
                    self.trades.append(record)
                    continue

                # Success
                record = TradeRecord(
                    ticket=ticket,
                    channel=signal.source_channel,
                    symbol=signal.symbol,
                    direction=signal.direction,
                    entry=entry_val,
                    sl=signal.stop_loss,
                    tp=tp_val,
                    tp_index=1,
                    lot_size=self.settings.lot_size,
                    status=TradeStatus.PENDING.value,
                    timestamp=now,
                    raw_signal=signal.raw_text[:200],
                    tp2=signal.take_profits[1] if len(signal.take_profits) >= 2 else 0,
                )
                self.trades.append(record)
                order_desc = "closer entry (fills first)" if label == "first" else "farther entry (breakeven)"
                report_lines.append(
                    f"  #{ticket} [{label}] Entry @ {entry_val} TP @ {tp_val} ({order_desc})"
                )

            # Link orders for breakeven: when first order hits TP, move second order SL to its entry
            if first_ticket and second_ticket:
                self._linked_orders[first_ticket] = {
                    "partner_ticket": second_ticket,
                    "breakeven_price": second_entry,  # move SL to second order's entry
                }
                self.logger.info(
                    f"Linked orders: #{first_ticket} (TP1) -> #{second_ticket} breakeven @ {second_entry}"
                )

            self._save_trades()
            report_lines.append(
                f"SL: {signal.stop_loss} | Lot: {self.settings.lot_size} each\n"
                f"When first order hits TP1, second order SL moves to entry (risk-free)"
            )
            await self._report("\n".join(report_lines))
            return

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
            tp2=signal.take_profits[1] if len(signal.take_profits) >= 2 else (signal.take_profits[0] if signal.take_profits else 0),
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

                    # Breakeven: if this trade has a linked partner, move partner SL to entry
                    if trade.status == TradeStatus.TP_HIT.value and trade.ticket in self._linked_orders:
                        link = self._linked_orders[trade.ticket]
                        partner_ticket = link["partner_ticket"]
                        breakeven_price = link["breakeven_price"]
                        self.logger.info(
                            f"TP hit on #{trade.ticket}, moving partner #{partner_ticket} "
                            f"SL to breakeven @ {breakeven_price}"
                        )
                        moved = self._modify_position_sl(partner_ticket, breakeven_price)
                        if moved:
                            await self._report(
                                f"🛡️ Breakeven applied:\n"
                                f"#{partner_ticket} SL moved to {breakeven_price} (entry)\n"
                                f"Position is now risk-free"
                            )
                        else:
                            self.logger.warning(
                                f"Failed to move SL for partner #{partner_ticket}"
                            )
                else:
                    # Might have been cancelled manually
                    trade.status = TradeStatus.CANCELLED.value
                    await self._report(
                        f"❌ Order CANCELLED (not found):\n"
                        f"#{trade.ticket} {trade.direction} {trade.symbol}"
                    )
                    updated = True

            elif trade.ticket in order_tickets:
                # Order still pending — check if price reached TP2
                # If market hits TP2 without the entry filling, cancel the order
                # Skip this check for dual-entry orders (BrianTradingForex) — they have their own TP
                if trade.tp2 > 0 and trade.channel != "@BrianTradingForex":
                    prices = self.mt5.get_symbol_price(trade.symbol)
                    if prices:
                        current_bid, current_ask = prices
                        tp2 = trade.tp2

                        should_cancel = False
                        if trade.direction == "SELL":
                            # For SELL: TP2 is below entry. If price drops to TP2
                            # without entry being hit, the move happened without us.
                            if current_bid <= tp2:
                                should_cancel = True
                        elif trade.direction == "BUY":
                            # For BUY: TP2 is above entry. If price rises to TP2
                            # without entry being hit, the move happened without us.
                            if current_ask >= tp2:
                                should_cancel = True

                        if should_cancel:
                            self.logger.info(
                                f"Price reached TP2 ({tp2}) for pending {trade.direction} "
                                f"order #{trade.ticket}. Cancelling order."
                            )
                            cancelled = self.mt5.cancel_order(trade.ticket)
                            if cancelled:
                                trade.status = TradeStatus.CANCELLED.value
                                updated = True
                                await self._report(
                                    f"🗑️ Order CANCELLED - price hit TP2 without filling:\n"
                                    f"#{trade.ticket} {trade.direction} {trade.symbol}\n"
                                    f"Entry: {trade.entry} (never filled)\n"
                                    f"TP2: {tp2} was reached\n"
                                    f"Source: {trade.channel}"
                                )
                            else:
                                self.logger.error(
                                    f"Failed to cancel order #{trade.ticket}"
                                )

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

    def _modify_position_sl(self, ticket: int, new_sl: float) -> bool:
        """Modify a position's stop loss by ticket."""
        if not self.mt5.ensure_connected():
            return False
        try:
            import MetaTrader5 as mt5
            # Find the position
            positions = mt5.positions_get(ticket=ticket)
            if not positions or len(positions) == 0:
                self.logger.warning(f"Position #{ticket} not found for SL modify")
                return False
            pos = positions[0]
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": ticket,
                "symbol": pos.symbol,
                "sl": round(new_sl, mt5.symbol_info(pos.symbol).digits),
                "tp": pos.tp,
            }
            result = mt5.order_send(request)
            if result is None:
                self.logger.error(f"SL modify returned None: {mt5.last_error()}")
                return False
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                self.logger.error(f"SL modify failed: retcode={result.retcode}")
                return False
            self.logger.info(f"SL modified for #{ticket}: {new_sl}")
            return True
        except Exception as e:
            self.logger.error(f"SL modify error: {e}")
            return False
