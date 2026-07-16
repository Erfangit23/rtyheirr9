"""
Telegram client — monitors signal channels using Telethon (user session).
Also runs the report bot simultaneously.
"""

import asyncio
import logging
import json
from typing import Optional, Callable, Awaitable

from telethon import TelegramClient, events
from telethon.tl.custom import Message

from signal_parser import parse_signal, Signal


class TelegramManager:
    """Manages both the signal monitor (user session) and the report bot."""

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_name: str,
        bot_token: str,
        channels: list[dict],
        authorized_user_ids: list[int],
        logger: Optional[logging.Logger] = None,
        settings=None,
    ):
        self.api_id = api_id
        self.api_hash = api_hash
        self.phone = phone
        self.session_name = session_name
        self.bot_token = bot_token
        self.channels = channels
        self.authorized_user_ids = authorized_user_ids
        self.logger = logger or logging.getLogger("xau_trader")
        self.settings = settings  # Reference to Settings for channel active check

        # User client for reading channel messages
        self.user_client: Optional[TelegramClient] = None
        # Bot client for sending reports and receiving commands
        self.bot_client: Optional[TelegramClient] = None

        # Callbacks
        self.on_signal_callback: Optional[Callable[[Signal], Awaitable[None]]] = None
        self.on_command_callback: Optional[Callable[[str, int], Awaitable[str]]] = None

        # Track processed message IDs to avoid duplicates
        self.processed_ids: set[int] = set()

    async def connect_user_client(self) -> bool:
        """Connect the user session for reading channels."""
        self.user_client = TelegramClient(
            self.session_name, self.api_id, self.api_hash
        )

        await self.user_client.connect()

        if not await self.user_client.is_user_authorized():
            self.logger.info("Telegram user session not authorized. Sending code...")
            await self.user_client.send_code_request(self.phone)
            code = input("Enter the Telegram code you received: ")
            try:
                await self.user_client.sign_in(self.phone, code)
            except Exception as e:
                self.logger.error(f"Telegram sign-in failed: {e}")
                return False

        me = await self.user_client.get_me()
        self.logger.info(f"Telegram user connected: {me.first_name} (@{me.username})")

        # Resolve channel entities
        for ch in self.channels:
            try:
                entity = await self.user_client.get_entity(ch["id"])
                ch["_entity"] = entity
                self.logger.info(f"Resolved channel: {ch['id']} -> {getattr(entity, 'title', ch['id'])}")
            except Exception as e:
                self.logger.warning(f"Could not resolve channel {ch['id']}: {e}")

        return True

    async def connect_bot_client(self) -> bool:
        """Connect the bot session for reports and commands."""
        self.bot_client = TelegramClient(
            "bot_session", self.api_id, self.api_hash
        )

        await self.bot_client.start(bot_token=self.bot_token)

        me = await self.bot_client.get_me()
        self.logger.info(f"Report bot connected: @{me.username}")

        # Register command handler
        @self.bot_client.on(events.NewMessage(incoming=True))
        async def bot_message_handler(event):
            sender = await event.get_sender()
            sender_id = sender.id

            # Only respond to authorized users
            if sender_id not in self.authorized_user_ids:
                return

            text = event.raw_text.strip()
            if not text:
                return

            self.logger.info(f"Bot command from {sender_id}: {text[:50]}...")

            if self.on_command_callback:
                response = await self.on_command_callback(text, sender_id)
                if response:
                    await event.reply(response)

        return True

    async def send_report(self, message: str, user_id: Optional[int] = None):
        """Send a report message via the bot."""
        if not self.bot_client:
            self.logger.warning("Bot client not connected; cannot send report.")
            return

        targets = [user_id] if user_id else self.authorized_user_ids
        for uid in targets:
            try:
                await self.bot_client.send_message(uid, message)
            except Exception as e:
                self.logger.error(f"Failed to send report to {uid}: {e}")

    def register_signal_handler(self, callback: Callable[[Signal], Awaitable[None]]):
        """Register the callback for when a signal is received."""
        self.on_signal_callback = callback

    def register_command_handler(self, callback: Callable[[str, int], Awaitable[str]]):
        """Register the callback for bot commands."""
        self.on_command_callback = callback

    async def start_monitoring(self):
        """Start listening for new messages in monitored channels."""
        if not self.user_client:
            self.logger.error("User client not connected; cannot start monitoring.")
            return

        @self.user_client.on(events.NewMessage(incoming=True))
        async def channel_message_handler(event):
            # Check if message is from one of our channels
            chat = await event.get_chat()
            chat_username = getattr(chat, "username", None)
            chat_id = getattr(chat, "id", None)

            matched_channel = None
            for ch in self.channels:
                if ch["id"].lstrip("@") == (chat_username or "") or ch["id"] == str(chat_id):
                    matched_channel = ch
                    break

            if not matched_channel:
                return

            # Check if channel is active
            if self.settings:
                ch_id = matched_channel["id"]
                if not self.settings.is_channel_active(ch_id):
                    self.logger.info(f"Channel {ch_id} is deactivated. Signal ignored.")
                    return

            msg_id = event.message.id
            if msg_id in self.processed_ids:
                return
            self.processed_ids.add(msg_id)

            text = event.raw_text
            self.logger.info(
                f"New message from {matched_channel['id']}: {text[:80]}..."
            )

            # Parse the signal
            signal = parse_signal(text, matched_channel["id"], matched_channel.get("format", "auto"))

            if signal:
                self.logger.info(f"Parsed signal: {signal}")
                if self.on_signal_callback:
                    try:
                        await self.on_signal_callback(signal)
                    except Exception as e:
                        self.logger.error(f"Signal callback error: {e}")
            else:
                self.logger.debug(f"Message did not match signal format: {text[:100]}")

        self.logger.info("Channel monitoring started.")
        self.logger.info(f"Monitoring {len(self.channels)} channels: {[ch['id'] for ch in self.channels]}")

        # Keep running
        await self.user_client.run_until_disconnected()

    async def run(self):
        """Run both user client (monitoring) and bot client (reports)."""
        if not await self.connect_user_client():
            return

        if not await self.connect_bot_client():
            self.logger.warning("Bot client failed to connect; reports will not be sent.")

        # Start monitoring in background
        monitoring_task = asyncio.create_task(self.start_monitoring())

        # Run bot client
        try:
            await self.bot_client.run_until_disconnected()
        finally:
            monitoring_task.cancel()

    async def disconnect(self):
        """Disconnect both clients."""
        if self.user_client:
            await self.user_client.disconnect()
        if self.bot_client:
            await self.bot_client.disconnect()
        self.logger.info("Telegram disconnected.")
