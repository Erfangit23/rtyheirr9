"""
Telegram report bot command handler.
Handles user commands for settings and reports.
"""

import logging
from settings import Settings
from mt5_connector import MT5Connector
from typing import Optional


class CommandHandler:
    """Processes commands sent to the report bot."""

    def __init__(
        self,
        settings: Settings,
        mt5: MT5Connector,
        trade_manager=None,
        logger: Optional[logging.Logger] = None,
    ):
        self.settings = settings
        self.mt5 = mt5
        self.trade_manager = trade_manager
        self.logger = logger or logging.getLogger("xau_trader")
        self._awaiting_password = {}  # user_id -> expected_action

    async def handle(self, text: str, user_id: int) -> str:
        """Handle a command from the user. Returns the response text."""
        text = text.strip()

        # --- Commands that don't need password ---
        if text.lower() == "/start":
            return (
                "🤖 XAU Trader Bot\n\n"
                "Commands:\n"
                "/status - Bot & MT5 status\n"
                "/settings - View current settings\n"
                "/channels - Channel stats & status\n"
                "/trades - Open positions & pending orders\nn"
                "/report - Today's trade summary\n\n"
                "🔐 To change settings, send:\n"
                "/change - Start settings change flow\n\n"
                "⚠️ Default password: Amin123"
            )

        if text.lower() == "/status":
            return self._cmd_status()

        if text.lower() == "/settings":
            return self._cmd_settings()

        if text.lower() == "/channels":
            return self._cmd_channels()

        if text.lower() == "/trades":
            return self._cmd_trades()

        if text.lower() == "/report":
            return self._cmd_report()

        if text.lower() == "/change":
            self._awaiting_password[user_id] = True
            return "🔐 Enter password to change settings:"

        # --- Password handling ---
        if user_id in self._awaiting_password:
            if text == self.settings.settings_password:
                self._awaiting_password.pop(user_id)
                self._awaiting_password[user_id] = "authenticated"
                return (
                    "✅ Authenticated!\n\n"
                    "What do you want to change?\n\n"
                    "1️⃣ lot <size> - Set lot size (e.g. lot 0.02)\n"
                    "2️⃣ tp <index> - Set TP index (1-7)\n"
                    "3️⃣ maxsl <pips> - Set max SL pips\n"
                    "4️⃣ dailysl <pips> - Set max daily SL pips\n"
                    "5️⃣ sleep - Pause the bot\n"
                    "6️⃣ wake - Resume the bot\n"
                    "7️⃣ chan on <@channel> - Activate channel\n"
                    "8️⃣ chan off <@channel> - Deactivate channel\n"
                    "9️⃣ done - Finish settings change"
                )
            else:
                self._awaiting_password.pop(user_id)
                return "❌ Wrong password. Access denied."

        # --- Authenticated commands ---
        if self._awaiting_password.get(user_id) == "authenticated":
            return self._handle_authed_command(text, user_id)

        return (
            "Unknown command. Send /start for help."
        )

    def _handle_authed_command(self, text: str, user_id: int) -> str:
        parts = text.lower().split()

        if parts[0] == "done":
            self._awaiting_password.pop(user_id)
            return "✅ Settings session ended."

        if parts[0] == "lot" and len(parts) >= 2:
            try:
                val = float(parts[1])
                if val <= 0:
                    return "❌ Lot size must be positive."
                self.settings.set_lot_size(val)
                return f"✅ Lot size set to {val}"
            except ValueError:
                return "❌ Invalid number. Example: lot 0.02"

        if parts[0] == "tp" and len(parts) >= 2:
            try:
                val = int(parts[1])
                if val < 1 or val > 10:
                    return "❌ TP index must be 1-10."
                self.settings.set_tp_index(val)
                return f"✅ Default TP set to TP{val}"
            except ValueError:
                return "❌ Invalid number. Example: tp 3"

        if parts[0] == "maxsl" and len(parts) >= 2:
            try:
                val = int(parts[1])
                if val < 1:
                    return "❌ Max SL must be positive."
                self.settings.set_max_sl_pips(val)
                return f"✅ Max SL per trade set to {val} pips"
            except ValueError:
                return "❌ Invalid number. Example: maxsl 200"

        if parts[0] == "dailysl" and len(parts) >= 2:
            try:
                val = int(parts[1])
                if val < 1:
                    return "❌ Max daily SL must be positive."
                self.settings.set_max_daily_sl_pips(val)
                return f"✅ Max daily SL set to {val} pips"
            except ValueError:
                return "❌ Invalid number. Example: dailysl 600"

        if parts[0] == "sleep":
            self.settings.set_bot_active(False)
            return "💤 Bot is now SLEEPING. No new trades will be placed."

        if parts[0] == "wake":
            self.settings.set_bot_active(True)
            return "☀️ Bot is now ACTIVE. Ready to trade."

        if parts[0] == "chan" and len(parts) >= 3:
            action = parts[1]
            channel_id = parts[2]
            # Normalize: ensure @ prefix
            if not channel_id.startswith("@"):
                channel_id = "@" + channel_id
            # Check channel exists
            ch = self.settings.get_channel_by_id(channel_id)
            if ch is None:
                return f"❌ Channel {channel_id} not found in config."
            if action == "on":
                self.settings.set_channel_active(channel_id, True)
                return f"✅ Channel {channel_id} ACTIVATED."
            elif action == "off":
                self.settings.set_channel_active(channel_id, False)
                return f"💤 Channel {channel_id} DEACTIVATED."
            else:
                return "❌ Use: chan on <@channel> or chan off <@channel>"

        return (
            "Unknown command. Options:\n"
            "lot <size> | tp <index> | maxsl <pips> | dailysl <pips> | sleep | wake | "
            "chan on <@channel> | chan off <@channel> | done"
        )

    def _cmd_status(self) -> str:
        active = "☀️ ACTIVE" if self.settings.bot_active else "💤 SLEEPING"
        mt5_ok = "✅ Connected" if self.mt5.connected else "❌ Disconnected"

        account = self.mt5.get_account_info()
        if account:
            acct_info = (
                f"Account: {account.login}\n"
                f"Balance: {account.balance:.2f} {account.currency}\n"
                f"Equity: {account.equity:.2f}\n"
                f"Margin: {account.margin:.2f}\n"
                f"Free Margin: {account.margin_free:.2f}\n"
                f"Leverage: 1:{account.leverage}"
            )
        else:
            acct_info = "Account info unavailable"

        return (
            f"📊 XAU Trader Bot Status\n\n"
            f"Bot: {active}\n"
            f"MT5: {mt5_ok}\n\n"
            f"{acct_info}"
        )

    def _cmd_settings(self) -> str:
        p = self.settings.get_all_trading_params()
        return (
            f"⚙️ Current Settings\n\n"
            f"Lot Size: {p['lot_size']}\n"
            f"Default TP: TP{p['default_tp_index']}\n"
            f"Max SL (per trade): {p['max_sl_pips']} pips\n"
            f"Max SL (per day): {p['max_daily_sl_pips']} pips\n"
            f"Bot Active: {'Yes' if p['bot_active'] else 'No'}\n\n"
            f"Send /change to modify (password required)"
        )

    def _cmd_trades(self) -> str:
        positions = self.mt5.get_open_positions()
        orders = self.mt5.get_pending_orders()

        lines = [f"📋 Open Positions ({len(positions)}):\n"]
        if positions:
            for p in positions:
                lines.append(
                    f"  #{p.ticket} {p.type} {p.volume} {p.symbol}\n"
                    f"    Entry: {p.price_open} | SL: {p.sl} | TP: {p.tp}\n"
                    f"    P/L: {p.profit:.2f}\n"
                )
        else:
            lines.append("  None\n")

        lines.append(f"\n📋 Pending Orders ({len(orders)}):\n")
        if orders:
            for o in orders:
                lines.append(
                    f"  #{o.ticket} type={o.type} {o.volume} {o.symbol}\n"
                    f"    Price: {o.price_open} | SL: {o.sl} | TP: {o.tp}\n"
                )
        else:
            lines.append("  None\n")

        return "".join(lines)

    def _cmd_report(self) -> str:
        summary = self.mt5.get_today_trade_summary()
        deals = summary.get("deals", [])
        total_loss = summary.get("total_loss_usd", 0.0)

        lines = [f"📈 Today's Trade Report\n\n"]
        lines.append(f"Total deals today: {len(deals)}\n")

        if deals:
            lines.append("\nDeals:\n")
            for d in deals:
                action = "BUY" if d.type == 0 else "SELL"
                profit_str = f"+{d.profit:.2f}" if d.profit >= 0 else f"{d.profit:.2f}"
                lines.append(
                    f"  #{d.ticket} {action} {d.volume} {d.symbol}\n"
                    f"    Price: {d.price} | Profit: {profit_str} {d.profit > 0 and '✅' or '❌'}\n"
                )
        else:
            lines.append("No deals today.\n")

        lines.append(f"\nTotal loss (losing deals): {total_loss:.2f} USD")
        return "".join(lines)

    def _cmd_channels(self) -> str:
        """Show channel status and per-channel trading stats."""
        channels = self.settings.channels
        if not channels:
            return "No channels configured."

        # Get per-channel stats from trade manager
        stats = {}
        if self.trade_manager:
            stats = self.trade_manager.get_channel_stats()

        lines = ["📡 Channels Status\n\n"]

        for ch in channels:
            ch_id = ch["id"]
            fmt = ch.get("format", "auto")
            is_active = ch.get("active", True)
            status_icon = "✅" if is_active else "💤"

            lines.append(f"{status_icon} {ch_id} ({fmt})")

            s = stats.get(ch_id, {})
            if s:
                total = s.get("total", 0)
                tp_hits = s.get("tp_hit", 0)
                sl_hits = s.get("sl_hit", 0)
                cancelled = s.get("cancelled", 0)
                rejected = s.get("rejected", 0)
                pending = s.get("pending", 0)
                filled = s.get("filled", 0)

                # Winrate: closed trades only (tp + sl)
                closed = tp_hits + sl_hits
                winrate = (tp_hits / closed * 100) if closed > 0 else 0

                lines.append(
                    f"   Trades: {total} | Pending: {pending} | Filled: {filled}\n"
                    f"   TP hits: {tp_hits} | SL hits: {sl_hits}\n"
                    f"   Cancelled: {cancelled} | Rejected: {rejected}\n"
                    f"   Winrate: {winrate:.1f}% ({closed} closed)\n"
                )
            else:
                lines.append("   No trades yet.\n")

            lines.append("")

        lines.append(
            "Send /change to activate/deactivate channels (password required)\n"
            "Then use: chan on <@channel> or chan off <@channel>"
        )

        return "".join(lines)
