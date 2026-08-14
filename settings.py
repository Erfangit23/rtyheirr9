"""
Settings manager — handles loading, saving, and runtime config updates.
"""

import json
import os
import threading
import logging
from typing import Optional, List, Dict


class Settings:
    """Thread-safe settings manager."""

    def __init__(self, config_path: str = "config.json", logger: Optional[logging.Logger] = None):
        self.config_path = config_path
        self.logger = logger or logging.getLogger("xau_trader")
        # Reentrant lock: several methods (advance_248_step, reset_248_multiplier,
        # set_channel_active) call self.save() while already holding the lock, and
        # save() re-acquires it. A plain Lock deadlocks there; RLock allows it.
        self._lock = threading.RLock()
        self._data: dict = {}
        self.load()

    def load(self):
        """Load config from file."""
        with self._lock:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                self.logger.info(f"Config loaded from {self.config_path}")
            except FileNotFoundError:
                self.logger.error(f"Config file not found: {self.config_path}")
                self._data = {}
            except json.JSONDecodeError as e:
                self.logger.error(f"Config JSON error: {e}")
                self._data = {}

    def save(self):
        """Save config to file, stripping non-serializable fields."""
        with self._lock:
            try:
                # Deep copy and strip non-serializable fields (like _entity from Telethon)
                import copy
                clean_data = copy.deepcopy(self._data)

                # Clean channels: remove _entity and any other non-serializable fields
                if "channels" in clean_data:
                    for ch in clean_data["channels"]:
                        if isinstance(ch, dict):
                            # Remove keys that start with _ (internal fields like _entity)
                            keys_to_remove = [k for k in ch if k.startswith("_")]
                            for k in keys_to_remove:
                                del ch[k]

                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(clean_data, f, indent=2, ensure_ascii=False)
                self.logger.info(f"Config saved to {self.config_path}")
            except Exception as e:
                self.logger.error(f"Failed to save config: {e}")

    # --- Telegram ---
    @property
    def telegram(self) -> dict:
        return self._data.get("telegram", {})

    @property
    def report_bot(self) -> dict:
        return self._data.get("report_bot", {})

    # --- MT5 ---
    @property
    def mt5(self) -> dict:
        return self._data.get("mt5", {})

    # --- Channels ---
    @property
    def channels(self) -> list:
        return self._data.get("channels", [])

    def get_channel_by_id(self, channel_id: str) -> Optional[dict]:
        """Find a channel config by its id."""
        for ch in self.channels:
            if ch.get("id") == channel_id:
                return ch
        return None

    def set_channel_active(self, channel_id: str, active: bool):
        """Activate or deactivate a channel."""
        with self._lock:
            for ch in self._data.get("channels", []):
                if ch.get("id") == channel_id:
                    ch["active"] = active
                    self.save()
                    return True
        return False

    def is_channel_active(self, channel_id: str) -> bool:
        """Check if a channel is active (default True if not set)."""
        ch = self.get_channel_by_id(channel_id)
        if ch is None:
            return False
        return ch.get("active", True)

    # --- Trading ---
    @property
    def trading(self) -> dict:
        return self._data.get("trading", {})

    @property
    def lot_size(self) -> float:
        return self.trading.get("lot_size", 0.01)

    @property
    def default_tp_index(self) -> int:
        return self.trading.get("default_tp_index", 2)

    @property
    def max_sl_pips(self) -> int:
        return self.trading.get("max_sl_pips", 150)

    @property
    def max_daily_sl_pips(self) -> int:
        return self.trading.get("max_daily_sl_pips", 500)

    @property
    def bot_active(self) -> bool:
        return self.trading.get("bot_active", True)

    @property
    def settings_password(self) -> str:
        return self.trading.get("settings_password", "Amin123")

    @property
    def ai_mode(self) -> bool:
        return self.trading.get("ai_mode", False)

    def set_ai_mode(self, value: bool):
        with self._lock:
            self._data.setdefault("trading", {})["ai_mode"] = value
        self.save()

    # --- 248 Mode (martingale lot doubling per channel on SL) ---
    # Default sequence: 1, 2, 4, 8, 16, 32, 64, 128 (powers of 2)
    DEFAULT_248_SEQ = [1, 2, 4, 8, 16, 32, 64, 128]
    FIBO_248_SEQ = [1, 2, 3, 5, 8, 13, 21, 34]

    # Channels that use Fibonacci sequence instead of doubling.
    # Empty since 2026-08: @Gulljanali17 now uses the standard doubling
    # sequence (1,2,4,8,...) like every other channel.
    FIBO_CHANNELS = []

    @property
    def mode_248(self) -> bool:
        return self.trading.get("mode_248", False)

    def set_mode_248(self, value: bool):
        with self._lock:
            self._data.setdefault("trading", {})["mode_248"] = value
            if not value:
                # Reset all channel steps when turning off
                self._data.setdefault("trading", {}).pop("mode_248_channels", None)
        self.save()

    @property
    def mode_248_channels(self) -> dict:
        """Returns {channel_id: step_index} for 248 mode."""
        return self.trading.get("mode_248_channels", {})

    def _get_248_sequence(self, channel_id: str) -> list:
        """Get the lot sequence for a channel."""
        if channel_id in self.FIBO_CHANNELS:
            return self.FIBO_248_SEQ
        return self.DEFAULT_248_SEQ

    def get_248_multiplier(self, channel_id: str) -> float:
        """Get current lot multiplier for a channel in 248 mode."""
        if not self.mode_248:
            return 1.0
        step = self.mode_248_channels.get(channel_id, 0)
        seq = self._get_248_sequence(channel_id)
        if step >= len(seq):
            return float(seq[-1])  # Cap at last value
        return float(seq[step])

    def advance_248_step(self, channel_id: str):
        """Advance to next step in the sequence (after SL hit)."""
        with self._lock:
            channels = self._data.setdefault("trading", {}).setdefault("mode_248_channels", {})
            current = channels.get(channel_id, 0)
            seq = self._get_248_sequence(channel_id)
            if current < len(seq) - 1:
                channels[channel_id] = current + 1
            # If already at max, stay there
            self.save()

    def reset_248_multiplier(self, channel_id: str):
        """Reset step to 0 for a channel (after TP hit)."""
        with self._lock:
            channels = self._data.setdefault("trading", {}).setdefault("mode_248_channels", {})
            channels[channel_id] = 0
            self.save()

    # --- Setters ---
    def set_lot_size(self, value: float):
        with self._lock:
            self._data.setdefault("trading", {})["lot_size"] = value
        self.save()

    def set_tp_index(self, value: int):
        with self._lock:
            self._data.setdefault("trading", {})["default_tp_index"] = value
        self.save()

    def set_max_sl_pips(self, value: int):
        with self._lock:
            self._data.setdefault("trading", {})["max_sl_pips"] = value
        self.save()

    def set_max_daily_sl_pips(self, value: int):
        with self._lock:
            self._data.setdefault("trading", {})["max_daily_sl_pips"] = value
        self.save()

    def set_bot_active(self, value: bool):
        with self._lock:
            self._data.setdefault("trading", {})["bot_active"] = value
        self.save()

    def get_all_trading_params(self) -> dict:
        """Return all trading parameters for display."""
        t = self.trading
        return {
            "lot_size": t.get("lot_size", 0.01),
            "default_tp_index": t.get("default_tp_index", 2),
            "max_sl_pips": t.get("max_sl_pips", 150),
            "max_daily_sl_pips": t.get("max_daily_sl_pips", 500),
            "bot_active": t.get("bot_active", True),
            "ai_mode": t.get("ai_mode", False),
        }
