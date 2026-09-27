"""
AI Signal Parser — uses NVIDIA DeepSeek V4 Flash to parse Telegram signals.

Handles:
- Trade signals (any format) -> extracts entry, SL, TPs, direction
- Cancel messages ("cancel last order", "ignore this")
- Modify messages ("move SL to 4050")
- Non-signal messages -> ignores

Falls back to regex parsers on API failure or timeout.
"""

import json
import logging
import os
import asyncio
from typing import Optional
from signal_parser import Signal, parse_signal as regex_parse

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None
    logging.error("openai package not installed. Run: pip install openai")


NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "deepseek-ai/deepseek-v4-flash"
# The API key is NEVER hardcoded in the source. Supply it either in
# config.json ("ai": {"api_key": "nvapi-..."} — config.json is gitignored)
# or through the NVIDIA_API_KEY environment variable.


SYSTEM_PROMPT = """You are a precise XAUUSD (Gold) trading signal parser. Your job is to read messages from Telegram trading channels and extract structured data.

You MUST return ONLY valid JSON — no markdown, no explanation, no extra text.

## Signal Types

### 1. Trade Signal
Extract: direction (BUY/SELL), entry price, stop_loss, and all take_profit levels.
If multiple entries are given (e.g. "Buy 4063 - 4060"), set entry to the first value and include all entry values in "entries".
If TP says "open" or no number, skip it.

Return format:
{"action": "trade", "direction": "BUY", "entry": 4050.0, "stop_loss": 4040.0, "take_profits": [4055.0, 4060.0, 4070.0], "entries": [4050.0]}
If only one entry, entries can be empty or contain just the one entry.

### 2. Cancel Signal
When the message says to cancel, close, or ignore a previous trade/order.
Examples: "Cancel order", "Close position", "Ignore last signal", "Dont open this"

Return format:
{"action": "cancel", "reason": "channel cancelled the order"}

### 3. Modify Signal
When the message changes SL or TP on an open trade.
Examples: "Move SL to 4050", "SL changed to 4060", "Update TP to 4080"

Return format:
{"action": "modify", "new_sl": 4050.0, "new_tp": null}

### 4. Not a Signal
For non-trading messages, news, analysis, or anything that is not actionable.

Return format:
{"action": "ignore", "reason": "not a trading signal"}

## Rules
- Symbol is always XAUUSD (Gold) — if message mentions other symbols, still parse but note it.
- Prices are always numbers (float). Remove any currency symbols, commas, or units.
- Direction must be exactly "BUY" or "SELL" (uppercase).
- If you cannot find entry or SL, return action "ignore".
- Take profits: extract ALL TP levels in order (TP1, TP2, TP3...). Skip "open" or non-numeric values.
- For Persian/Farsi text (مثل: اسکلپ, خرید, فروش), still parse normally.
- Return ONLY the JSON object, nothing else."""

# Timeout for AI API calls (seconds)
AI_TIMEOUT = 8


class AIParser:
    """Parses Telegram messages using NVIDIA DeepSeek V4 Flash API."""

    def __init__(self, logger: Optional[logging.Logger] = None,
                 api_key: Optional[str] = None):
        self.logger = logger or logging.getLogger("xau_trader")
        self.client = None
        key = (api_key or os.environ.get("NVIDIA_API_KEY", "")).strip()
        if not OpenAI:
            self.logger.warning("OpenAI SDK not installed. AI parser unavailable.")
        elif not key:
            self.logger.warning(
                "AI parser unavailable: no API key. Put it in config.json as "
                '\"ai\": {\"api_key\": \"nvapi-...\"} or set the NVIDIA_API_KEY '
                "environment variable. Falling back to regex parsing."
            )
        else:
            self.client = OpenAI(base_url=NVIDIA_BASE_URL, api_key=key)
            self.logger.info("AI Parser initialized with NVIDIA DeepSeek V4 Flash")

    def is_available(self) -> bool:
        """Check if AI parser is available."""
        return self.client is not None

    def parse_message(self, text: str, channel: str) -> Optional[dict]:
        """
        Parse a Telegram message using AI.

        Returns a dict with "action" and relevant fields, or None on failure.
        """
        if not self.client:
            return None

        try:
            response = self.client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Channel: {channel}\n\nMessage:\n{text}"},
                ],
                temperature=0.1,  # Low temperature for consistent parsing
                top_p=0.95,
                max_tokens=1024,  # Signals are short, no need for 16k
                extra_body={
                    "chat_template_kwargs": {
                        "thinking": False,  # Disable thinking for speed
                    }
                },
                stream=False,
                timeout=AI_TIMEOUT,
            )

            content = response.choices[0].message.content.strip()

            # Clean up any markdown wrapping
            if content.startswith("```"):
                content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            # Parse JSON
            result = json.loads(content)

            self.logger.info(
                f"AI parsed: action={result.get('action')} "
                f"direction={result.get('direction', 'N/A')} "
                f"entry={result.get('entry', 'N/A')}"
            )

            return result

        except json.JSONDecodeError as e:
            self.logger.warning(f"AI returned invalid JSON: {e}")
            return None
        except Exception as e:
            self.logger.warning(f"AI parse error: {e}")
            return None

    def parse_signal(self, text: str, channel: str) -> Optional[Signal]:
        """
        Parse a message and return a Signal object if it's a trade signal.
        Falls back to regex parser if AI fails.

        Returns None for non-trade messages (cancel, modify, ignore).
        """
        # Try AI first
        result = self.parse_message(text, channel)

        if result is None:
            # AI failed — fall back to regex
            self.logger.info("AI failed, falling back to regex parser")
            return regex_parse(text, channel, "auto")

        action = result.get("action", "ignore")

        if action != "trade":
            self.logger.info(f"AI says action={action}, not a trade signal")
            return None

        # Validate required fields
        direction = result.get("direction", "").upper()
        entry = result.get("entry")
        stop_loss = result.get("stop_loss")
        take_profits = result.get("take_profits", [])

        if not direction or direction not in ("BUY", "SELL"):
            self.logger.warning(f"AI returned invalid direction: {direction}")
            return regex_parse(text, channel, "auto")

        if entry is None or stop_loss is None:
            self.logger.warning("AI missing entry or stop_loss")
            return regex_parse(text, channel, "auto")

        if not take_profits:
            self.logger.warning("AI returned no take_profits")
            return regex_parse(text, channel, "auto")

        # Ensure all values are floats
        try:
            entry = float(entry)
            stop_loss = float(stop_loss)
            take_profits = [float(tp) for tp in take_profits]
        except (ValueError, TypeError) as e:
            self.logger.warning(f"AI returned non-numeric prices: {e}")
            return regex_parse(text, channel, "auto")

        entries = result.get("entries", [])
        if entries:
            entries = [float(e) for e in entries]

        return Signal(
            symbol="XAUUSD",
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profits=take_profits,
            raw_text=text,
            source_channel=channel,
            entries=entries if entries else [],
        )

    def parse_action(self, text: str, channel: str) -> Optional[dict]:
        """
        Parse a message for ANY action (trade, cancel, modify, ignore).
        Used to detect cancel/modify messages that regex cannot handle.

        Returns the raw action dict, or None on failure.
        """
        result = self.parse_message(text, channel)

        if result is None:
            # AI failed — can't detect cancel/modify, return None
            return None

        return result
