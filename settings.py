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
        # Reentrant lock: several methods (set_channel_active, set_ai_mode,
        # set_filter_enabled) call self.save() while already holding the lock,
        # and save() re-acquires it. A plain Lock deadlocks there; RLock allows it.
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

    # --- SL cooldown (pause a channel after a real SL hit) ---
    @property
    def sl_cooldown_minutes(self) -> int:
        """Minutes to pause a channel after a real SL hit. 0 disables it."""
        try:
            return int(self.trading.get("sl_cooldown_minutes", 90))
        except (TypeError, ValueError):
            return 90

    def set_sl_cooldown_minutes(self, value: int):
        with self._lock:
            self._data.setdefault("trading", {})["sl_cooldown_minutes"] = int(value)
        self.save()

    @property
    def ai_mode(self) -> bool:
        return self.trading.get("ai_mode", False)

    @property
    def ai_api_key(self) -> str:
        """NVIDIA API key for AI parsing/commentary.

        Comes from config.json ("ai": {"api_key": "..."}) or the
        NVIDIA_API_KEY environment variable. Never hardcoded in the source,
        so the repo stays safe to publish.
        """
        return ((self._data.get("ai", {}) or {}).get("api_key")
                or os.environ.get("NVIDIA_API_KEY", "")).strip()

    def set_ai_mode(self, value: bool):
        with self._lock:
            self._data.setdefault("trading", {})["ai_mode"] = value
        self.save()

    # --- Max concurrent open trades (account safety cap) ---
    # 0 disables the cap. Counts open positions + pending orders on XAUUSD.
    @property
    def max_open_trades(self) -> int:
        return self.trading.get("max_open_trades", 5)

    def set_max_open_trades(self, value: int):
        with self._lock:
            self._data.setdefault("trading", {})["max_open_trades"] = value
        self.save()

    # --- Signal validation filters (EMA200 / RSI / ATR SL floor) ---
    # Defaults are deliberately LENIENT so the filters rarely block; tune via
    # dry-run reports before enforcing. Per-channel overrides live in the
    # channel config as "filters": false (skip all) or a dict like
    # {"rsi": false, "ema200": {"buffer_atr_mult": 0.5}}.
    VALIDATION_DEFAULTS = {
        "mode": "dry_run",
        "ema200": {
            "enabled": True,
            "timeframe": "H1",
            "buffer_atr_mult": 0.3,   # neutral zone around the EMA (x ATR H1)
            "ext_guard": False,        # reject over-extended entries (off by default)
            "ext_guard_atr_mult": 2.5,
        },
        "rsi": {
            "enabled": True,
            "timeframe": "M15",
            "period": 14,
            "buy_max": 75,   # reject BUY at/above (exhaustion)
            "sell_min": 25,  # reject SELL at/below
        },
        "atr_sl": {
            "enabled": True,
            "timeframe": "M15",
            "period": 14,
            "min_sl_atr_mult": 0.5,  # reject SL tighter than 0.5 x ATR
        },
    }
    FILTER_NAMES = ("ema200", "rsi", "atr_sl")

    def get_validation_mode(self) -> str:
        """off | dry_run | on"""
        return str(self._data.get("validation", {}).get("mode", "dry_run")).lower()

    def set_validation_mode(self, mode: str):
        with self._lock:
            self._data.setdefault("validation", {})["mode"] = mode
        self.save()

    def channel_filters_disabled(self, channel_id: str) -> bool:
        """Channel config has "filters": false -> validator skips the channel."""
        ch = self.get_channel_by_id(channel_id)
        return isinstance(ch, dict) and ch.get("filters") is False

    def filter_config_for_channel(self, channel_id: str, name: str):
        """Merged filter config (defaults <- validation block <- channel
        override). Returns None when the filter is disabled for the channel."""
        base = dict(self.VALIDATION_DEFAULTS.get(name, {}))
        base.update(self._data.get("validation", {}).get(name, {}) or {})
        ch = self.get_channel_by_id(channel_id)
        if isinstance(ch, dict):
            ov = ch.get("filters")
            if ov is False:
                return None
            if isinstance(ov, dict):
                o = ov.get(name)
                if o is False:
                    return None
                if o is True:
                    base["enabled"] = True  # channel re-enables a globally-off filter
                elif isinstance(o, dict):
                    base.update(o)
        return base

    def channel_filter_overrides(self) -> dict:
        """Channels that carry a per-channel filters override, for display."""
        out = {}
        for ch in self._data.get("channels", []):
            if not isinstance(ch, dict):
                continue
            if isinstance(ch.get("filters"), dict):
                out[ch.get("id")] = ch["filters"]
            elif ch.get("filters") is False:
                out[ch.get("id")] = False
        return out

    def set_ema_buffer(self, mult: float):
        """EMA200 neutral zone, in ATR units (0 = strictest: BUY only above the
        EMA, SELL only below)."""
        with self._lock:
            self._data.setdefault("validation", {}).setdefault("ema200", {})["buffer_atr_mult"] = mult
        self.save()

    def set_rsi_thresholds(self, buy_max: float, sell_min: float):
        """Reject BUY at/above buy_max and SELL at/below sell_min."""
        with self._lock:
            v = self._data.setdefault("validation", {}).setdefault("rsi", {})
            v["buy_max"] = buy_max
            v["sell_min"] = sell_min
        self.save()

    def set_atr_floor(self, mult: float):
        """Minimum SL distance in ATR(M15) units — stops inside it are rejected."""
        with self._lock:
            self._data.setdefault("validation", {}).setdefault("atr_sl", {})["min_sl_atr_mult"] = mult
        self.save()

    def set_filter_enabled(self, name: str, enabled: bool, channel_id: str = None):
        """Toggle a filter globally or for one channel (from Telegram)."""
        with self._lock:
            if channel_id:
                for ch in self._data.get("channels", []):
                    if ch.get("id") == channel_id:
                        if not isinstance(ch.get("filters"), dict):
                            ch["filters"] = {}
                        ch["filters"][name] = enabled
                        break
            else:
                self._data.setdefault("validation", {}).setdefault(name, {})["enabled"] = enabled
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
            "max_open_trades": t.get("max_open_trades", 5),
            "sl_cooldown_minutes": t.get("sl_cooldown_minutes", 90),
            "bot_active": t.get("bot_active", True),
            "ai_mode": t.get("ai_mode", False),
        }
