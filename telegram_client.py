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
from ai_parser import AIParser


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
        self.on_cancel_callback: Optional[Callable[[str], Awaitable[None]]] = None
        self.on_modify_callback: Optional[Callable[[str, Optional[float], Optional[float]], Awaitable[None]]] = None

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

    def register_cancel_handler(self, callback: Callable[[str], Awaitable[None]]):
        """Register the callback for cancel messages (AI mode only)."""
        self.on_cancel_callback = callback

    def register_modify_handler(self, callback: Callable[[str, Optional[float], Optional[float]], Awaitable[None]]):
        """Register the callback for modify messages (AI mode only)."""
        self.on_modify_callback = callback

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

            # Get text — handle forwarded messages properly
            text = event.raw_text or ""
            # For forwarded messages, also check fwd_from_text
            if not text and hasattr(event.message, 'fwd_from') and event.message.fwd_from:
                text = getattr(event.message.fwd_from, 'message', '') or ""
            # Also try getting text from the message object directly
            if not text and event.message.text:
                text = event.message.text
            if not text and event.message.message:
                text = event.message.message

            if not text or len(text.strip()) < 10:
                self.logger.debug(
                    f"Message {msg_id} from {matched_channel['id']} has no usable text, skipping"
                )
                return

            self.logger.info(
                f"New message from {matched_channel['id']}: {text[:80]}..."
            )

            # Parse the signal
            fmt = matched_channel.get("format", "auto")
            signal = None

            # Check if AI mode is enabled
            # Some channels use regex for signal parsing but AI for cancel/modify detection
            regex_only_channels = ["@Gulljanali17", "@BrianTradingForex", "@forexkhan", "@GoldVisionofficial", "@Eliz_fxac_ademy1"]
            use_ai = self.settings and self.settings.ai_mode
            use_ai_for_parsing = use_ai and matched_channel["id"] not in regex_only_channels

            if use_ai:
                # Try AI parser first (for cancel/modify detection on all channels)
                if not hasattr(self, '_ai_parser'):
                    self._ai_parser = AIParser(logger=self.logger)

                if self._ai_parser.is_available():
                    # For regex-only channels, still check for cancel/modify via AI
                    # but don't use AI for signal parsing
                    if not use_ai_for_parsing:
                        action = self._ai_parser.parse_action(text, matched_channel["id"])
                        if action:
                            action_type = action.get("action", "ignore")
                            if action_type == "cancel":
                                self.logger.info(f"AI detected CANCEL action: {action.get('reason', '')}")
                                if self.on_cancel_callback:
                                    await self.on_cancel_callback(matched_channel["id"])
                                return
                            elif action_type == "modify":
                                self.logger.info(f"AI detected MODIFY action: {action}")
                                if self.on_modify_callback:
                                    await self.on_modify_callback(
                                        matched_channel["id"],
                                        action.get("new_sl"),
                                        action.get("new_tp"),
                                    )
                                return
                            elif action_type == "ignore":
                                # Not a cancel/modify — fall through to regex parsing
                                pass
                        # Fall through to regex parsing below
                    else:
                        # Full AI parsing for this channel
                        action = self._ai_parser.parse_action(text, matched_channel["id"])
                        if action:
                            action_type = action.get("action", "ignore")
                            if action_type == "cancel":
                                self.logger.info(f"AI detected CANCEL action: {action.get('reason', '')}")
                                if self.on_cancel_callback:
                                    await self.on_cancel_callback(matched_channel["id"])
                                return
                            elif action_type == "modify":
                                self.logger.info(f"AI detected MODIFY action: {action}")
                                if self.on_modify_callback:
                                    await self.on_modify_callback(
                                        matched_channel["id"],
                                        action.get("new_sl"),
                                        action.get("new_tp"),
                                    )
                                return
                            elif action_type == "ignore":
                                self.logger.debug(f"AI says ignore: {action.get('reason', '')}")
                                # Don't return — fall through to regex parsing
                                # (AI might have misidentified a signal as non-signal)
                                pass

                        signal = self._ai_parser.parse_signal(text, matched_channel["id"])
                else:
                    self.logger.warning("AI mode on but parser unavailable, using regex")
                    signal = parse_signal(text, matched_channel["id"], fmt)

            if not signal:
                # Regex fallback (also used when AI is off)
                signal = parse_signal(text, matched_channel["id"], fmt)
                if not signal and fmt != "auto":
                    signal = parse_signal(text, matched_channel["id"], "auto")

            if signal:
                self.logger.info(f"Parsed signal: {signal}")
                if self.on_signal_callback:
                    try:
                        await self.on_signal_callback(signal)
                    except Exception as e:
                        self.logger.error(f"Signal callback error: {e}")
            elif not use_ai_for_parsing:
                self.logger.warning(
                    f"Message did not match any signal format: {text[:200]}"
                )

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
