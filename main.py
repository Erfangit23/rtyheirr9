"""
XAU Trader Bot — Main Entry Point

This bot:
1. Connects to Telegram (user session) to monitor signal channels
2. Connects to MetaTrader 5 for order execution
3. Runs a Telegram bot for reporting and settings management
4. Runs 24/7 on a Windows VPS

Usage: python main.py
"""

import asyncio
import sys
import signal as sig
import logging

from logger_util import setup_logger
from settings import Settings
from mt5_connector import MT5Connector
from telegram_client import TelegramManager
from trade_manager import TradeManager
from bot_commands import CommandHandler


async def main():
    logger = setup_logger()
    logger.info("=" * 60)
    logger.info("XAU Trader Bot — Starting")
    logger.info("=" * 60)

    # Load configuration
    settings = Settings("config.json", logger=logger)

    if not settings.telegram or not settings.telegram.get("api_id"):
        logger.error(
            "Telegram credentials not configured. "
            "Edit config.json before running."
        )
        print("\n❌ ERROR: Edit config.json with your Telegram api_id, api_hash, and phone first!")
        print("   Get api_id/api_hash from: https://my.telegram.org/apps\n")
        sys.exit(1)

    if not settings.mt5 or not settings.mt5.get("login"):
        logger.error(
            "MT5 credentials not configured. "
            "Edit config.json before running."
        )
        print("\n❌ ERROR: Edit config.json with your MT5 login, password, and server first!")
        sys.exit(1)

    if not settings.report_bot or not settings.report_bot.get("bot_token"):
        logger.error(
            "Report bot token not configured. "
            "Edit config.json before running."
        )
        print("\n❌ ERROR: Edit config.json with your report bot token first!")
        print("   Create a bot with @BotFather to get a token.\n")
        sys.exit(1)

    # Connect to MT5
    mt5_config = settings.mt5
    mt5_conn = MT5Connector(
        login=mt5_config["login"],
        password=mt5_config["password"],
        server=mt5_config["server"],
        terminal_path=mt5_config.get("terminal_path", ""),
        logger=logger,
    )

    if not mt5_conn.connect():
        logger.error("Failed to connect to MT5. Ensure MetaTrader 5 is running.")
        print("\n❌ ERROR: Cannot connect to MetaTrader 5.")
        print("   Make sure MT5 is installed and running on this VPS.\n")
        sys.exit(1)

    # Initialize trade manager
    trade_manager = TradeManager(
        settings=settings,
        mt5=mt5_conn,
        logger=logger,
    )

    # Initialize Telegram manager
    tg_config = settings.telegram
    bot_config = settings.report_bot

    tg_manager = TelegramManager(
        api_id=tg_config["api_id"],
        api_hash=tg_config["api_hash"],
        phone=tg_config["phone"],
        session_name=tg_config.get("session_name", "trader_session"),
        bot_token=bot_config["bot_token"],
        channels=settings.channels,
        authorized_user_ids=bot_config.get("authorized_user_ids", []),
        logger=logger,
        settings=settings,
    )

    # Set up the report callback (sends messages via bot)
    async def report_callback(message: str):
        await tg_manager.send_report(message)

    trade_manager.report_callback = report_callback

    # Set up AI commentary callback (sends to @The_usdt_hunter via bot DM)
    COMMENTARY_TARGET_USERNAME = "The_usdt_hunter"
    _commentary_user_id = None

    async def commentary_callback(message: str):
        nonlocal _commentary_user_id
        try:
            if _commentary_user_id is None:
                # Resolve the username to a user ID via bot client
                entity = await tg_manager.bot_client.get_entity(COMMENTARY_TARGET_USERNAME)
                _commentary_user_id = entity.id
                logger.info(f"Commentary target @{COMMENTARY_TARGET_USERNAME} resolved to ID {_commentary_user_id}")
            await tg_manager.bot_client.send_message(_commentary_user_id, message)
        except Exception as e:
            logger.error(f"Commentary send error: {e}")

    trade_manager.commentary_callback = commentary_callback

    # Set up signal handler -> trade manager
    async def signal_callback(signal_obj):
        await trade_manager.process_signal(signal_obj)

    tg_manager.register_signal_handler(signal_callback)

    # Set up cancel handler (AI mode only — cancels last pending order from channel)
    async def cancel_callback(channel: str):
        logger.info(f"Cancel signal from {channel}")
        # Find the most recent pending order from this channel
        for trade in reversed(trade_manager.trades):
            if trade.channel == channel and trade.status == "pending":
                if trade.ticket > 0:
                    cancelled = mt5_conn.cancel_order(trade.ticket)
                    if cancelled:
                        trade.status = "cancelled"
                        trade_manager._save_trades()
                        await tg_manager.send_report(
                            f"🗑️ Order CANCELLED (channel cancel signal):\n"
                            f"#{trade.ticket} {trade.direction} {trade.symbol}\n"
                            f"Source: {channel}"
                        )
                break

    tg_manager.register_cancel_handler(cancel_callback)

    # Set up modify handler (AI mode only — modifies SL/TP on open position)
    async def modify_callback(channel: str, new_sl, new_tp):
        logger.info(f"Modify signal from {channel}: SL={new_sl}, TP={new_tp}")
        for trade in reversed(trade_manager.trades):
            if trade.channel == channel and trade.status == "filled":
                if new_sl is not None:
                    moved = trade_manager._modify_position_sl(trade.ticket, float(new_sl))
                    if moved:
                        trade.sl = float(new_sl)
                        trade_manager._save_trades()
                        await tg_manager.send_report(
                            f"🔧 SL modified (channel signal):\n"
                            f"#{trade.ticket} {trade.direction} {trade.symbol}\n"
                            f"New SL: {new_sl}\n"
                            f"Source: {channel}"
                        )
                break

    tg_manager.register_modify_handler(modify_callback)

    # Set up command handler
    # tg_manager.user_client will be set after connect_user_client(),
    # but we pass a lambda that retrieves it lazily
    class TgClientProxy:
        """Lazy proxy to get the telethon user client after it's connected."""
        def __init__(self, tg_manager):
            self._tg_manager = tg_manager
        def __getattr__(self, name):
            if self._tg_manager.user_client:
                return getattr(self._tg_manager.user_client, name)
            raise AttributeError("User client not connected")

    tg_client_proxy = TgClientProxy(tg_manager)

    command_handler = CommandHandler(
        settings=settings,
        mt5=mt5_conn,
        trade_manager=trade_manager,
        tg_user_client=tg_client_proxy,
        logger=logger,
    )

    async def command_callback(text: str, user_id: int) -> str:
        return await command_handler.handle(text, user_id)

    tg_manager.register_command_handler(command_callback)

    # Start trade update checker (runs every 5 seconds for fast breakeven detection)
    async def trade_update_loop():
        while True:
            try:
                await trade_manager.check_trade_updates()
            except Exception as e:
                logger.error(f"Trade update check error: {e}")
            await asyncio.sleep(5)

    # Start MT5 keepalive (reconnect if needed every 60 seconds)
    async def mt5_keepalive_loop():
        while True:
            try:
                mt5_conn.ensure_connected()
            except Exception as e:
                logger.error(f"MT5 keepalive error: {e}")
            await asyncio.sleep(60)

    # Handle graceful shutdown
    shutdown_event = asyncio.Event()

    def shutdown_signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        shutdown_event.set()

    # On Windows, SIGINT works; SIGTERM may not
    try:
        sig.signal(sig.SIGINT, shutdown_signal_handler)
        sig.signal(sig.SIGTERM, shutdown_signal_handler)
    except (ValueError, OSError):
        pass

    logger.info("All systems ready. Starting monitoring loops...")

    # Run everything concurrently
    tasks = [
        asyncio.create_task(tg_manager.run(), name="telegram"),
        asyncio.create_task(trade_update_loop(), name="trade_updates"),
        asyncio.create_task(mt5_keepalive_loop(), name="mt5_keepalive"),
    ]

    # Wait for shutdown or task failure
    done, pending = await asyncio.wait(
        tasks + [asyncio.create_task(shutdown_event.wait(), name="shutdown")],
        return_when=asyncio.FIRST_COMPLETED,
    )

    # Cancel remaining tasks
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Cleanup
    logger.info("Disconnecting...")
    await tg_manager.disconnect()
    mt5_conn.disconnect()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
    except Exception as e:
        logging.getLogger("xau_trader").fatal(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
