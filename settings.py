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
        self._lock = threading.Lock()
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
        """Save config to file."""
        with self._lock:
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
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
        }
