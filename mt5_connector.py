"""
MetaTrader 5 connector — handles connection, order placement, and account info.
"""

import logging
from typing import Optional
from signal_parser import Signal

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None
    logging.error(
        "MetaTrader5 package not installed. Run: pip install MetaTrader5"
    )


class MT5Connector:
    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        terminal_path: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.logger = logger or logging.getLogger("xau_trader")
        self.connected = False

    def connect(self) -> bool:
        """Initialize MT5 connection."""
        if mt5 is None:
            self.logger.error("MetaTrader5 library not available.")
            return False

        kwargs = {}
        if self.terminal_path:
            kwargs["path"] = self.terminal_path

        if not mt5.initialize(**kwargs):
            self.logger.error(f"MT5 initialize() failed: {mt5.last_error()}")
            return False

        authorized = mt5.login(login=self.login, password=self.password, server=self.server)
        if not authorized:
            self.logger.error(
                f"MT5 login failed: login={self.login} server={self.server} "
                f"error={mt5.last_error()}"
            )
            mt5.shutdown()
            return False

        self.connected = True
        info = mt5.account_info()
        if info:
            self.logger.info(
                f"MT5 connected: {info.name} | Balance: {info.balance} "
                f"{info.currency} | Leverage: 1:{info.leverage}"
            )
        return True

    def disconnect(self):
        """Shut down MT5 connection."""
        if mt5 and self.connected:
            mt5.shutdown()
            self.connected = False
            self.logger.info("MT5 disconnected.")

    def ensure_connected(self) -> bool:
        """Ensure MT5 is connected; reconnect if needed."""
        if self.connected:
            try:
                # Quick check
                info = mt5.account_info()
                if info is not None:
                    return True
            except Exception:
                self.connected = False

        return self.connect()

    def get_symbol_price(self, symbol: str = "XAUUSD") -> Optional[tuple]:
        """Return (bid, ask) for the symbol."""
        if not self.ensure_connected():
            return None

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            self.logger.error(f"Failed to get tick for {symbol}")
            return None

        return (tick.bid, tick.ask)

    def place_limit_order(
        self,
        signal: Signal,
        lot_size: float,
        tp_index: int = 2,
        max_sl_pips: int = 150,
    ) -> Optional[int]:
        """
        Place a limit order based on the signal.

        - Uses entry as the limit price.
        - Uses the TP at tp_index (1-based) as take profit.
        - Uses the signal's stop loss.
        - Validates SL distance does not exceed max_sl_pips.

        Returns the order ticket (request result order) or None on failure.
        """
        if not self.ensure_connected():
            return None

        symbol = signal.symbol
        direction = signal.direction.upper()

        # Ensure symbol is available
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            self.logger.error(f"Symbol {symbol} not found in MT5.")
            return None

        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                self.logger.error(f"Failed to select symbol {symbol}.")
                return None

        # Determine order type
        if direction == "BUY":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT
        elif direction == "SELL":
            order_type = mt5.ORDER_TYPE_SELL_LIMIT
        else:
            self.logger.error(f"Unknown direction: {direction}")
            return None

        # Get TP
        tp_index = max(1, min(tp_index, len(signal.take_profits)))
        tp_price = signal.take_profits[tp_index - 1]

        # Validate SL distance
        sl_distance_pips = abs(signal.entry - signal.stop_loss)
        # For gold (XAUUSD), 1 pip = 0.1 (standard) or we use point-based
        # Gold typically: 1 pip = $0.10 movement, so pips = distance / 0.1
        point = symbol_info.point
        # Using the standard: for gold, pips = price_diff / 0.1
        # But many brokers use different point sizes. We'll use: pips = diff / (point * 10)
        pip_size = point * 10 if point < 0.01 else 0.1
        sl_distance_pips_calc = abs(signal.entry - signal.stop_loss) / pip_size

        self.logger.info(
            f"SL distance check: {abs(signal.entry - signal.stop_loss)} price units "
            f"= {sl_distance_pips_calc:.1f} pips (max: {max_sl_pips})"
        )

        if sl_distance_pips_calc > max_sl_pips:
            self.logger.warning(
                f"SL distance {sl_distance_pips_calc:.1f} pips exceeds max {max_sl_pips} pips. "
                f"Order NOT placed."
            )
            return -1  # Special: rejected due to SL limit

        # Normalize prices
        digits = symbol_info.digits
        entry_price = round(signal.entry, digits)
        sl_price = round(signal.stop_loss, digits)
        tp_price_norm = round(tp_price, digits)

        # Adjust entry price 10 pips closer to market to increase fill probability.
        # This compensates for broker price differences.
        # 10 pips on gold (XAUUSD) = 1.0 price unit (1 pip = 0.1)
        pip_adjustment = 1.0  # 10 pips in price units
        if direction == "SELL":
            # SELL LIMIT: lower the entry by 10 pips (closer to current market)
            adjusted_entry = round(entry_price - pip_adjustment, digits)
            self.logger.info(
                f"Entry adjusted -10 pips for fill: {entry_price} -> {adjusted_entry}"
            )
            entry_price = adjusted_entry
        elif direction == "BUY":
            # BUY LIMIT: raise the entry by 10 pips (closer to current market)
            adjusted_entry = round(entry_price + pip_adjustment, digits)
            self.logger.info(
                f"Entry adjusted +10 pips for fill: {entry_price} -> {adjusted_entry}"
            )
            entry_price = adjusted_entry

        # Fill mode
        filling = mt5.symbol_info(symbol)
        filling_type = mt5.ORDER_FILLING_RETURN
        if filling:
            filling_type = filling.filling_mode

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": entry_price,
            "sl": sl_price,
            "tp": tp_price_norm,
            "deviation": 20,
            "magic": 779900,
            "comment": f"XAU-Bot|{signal.source_channel}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_type,
        }

        self.logger.info(
            f"Placing limit order: {direction} {symbol} "
            f"vol={lot_size} price={entry_price} "
            f"sl={sl_price} tp={tp_price_norm} (TP#{tp_index})"
        )

        result = mt5.order_send(request)

        if result is None:
            self.logger.error(f"order_send returned None: {mt5.last_error()}")
            return None

        if result.retcode != mt5.TRADE_RETCODE_DONE and result.retcode != mt5.TRADE_RETCODE_PLACED:
            self.logger.error(
                f"Order failed: retcode={result.retcode} "
                f"comment={result.comment}"
            )
            return None

        self.logger.info(
            f"Order placed successfully: ticket={result.order} "
            f"retcode={result.retcode}"
        )
        return result.order

    def cancel_order(self, ticket: int) -> bool:
        """Cancel a pending order by ticket."""
        if not self.ensure_connected():
            return False

        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": ticket,
        }

        result = mt5.order_send(request)
        if result is None:
            self.logger.error(f"Cancel order returned None: {mt5.last_error()}")
            return False

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            self.logger.error(f"Cancel failed: retcode={result.retcode}")
            return False

        self.logger.info(f"Order {ticket} cancelled.")
        return True

    def get_open_positions(self) -> list:
        """Get all open positions."""
        if not self.ensure_connected():
            return []
        return mt5.positions_get(symbol="XAUUSD") or []

    def get_pending_orders(self) -> list:
        """Get all pending orders."""
        if not self.ensure_connected():
            return []
        return mt5.orders_get(symbol="XAUUSD") or []

    def get_account_info(self):
        """Get account information."""
        if not self.ensure_connected():
            return None
        return mt5.account_info()

    def get_today_trade_summary(self) -> dict:
        """Get summary of today's deals for daily SL tracking."""
        if not self.ensure_connected():
            return {"deals": [], "total_loss_pips": 0.0}

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc)
        utc_from = today.replace(hour=0, minute=0, second=0, microsecond=0)

        deals = mt5.history_deals_get(utc_from, today)
        if deals is None:
            return {"deals": [], "total_loss_pips": 0.0}

        total_loss = 0.0
        deal_list = []
        for deal in deals:
            if deal.magic != 779900:
                continue
            deal_list.append(deal)
            if deal.profit < 0:
                # Estimate pips from the deal
                # This is approximate; for precise tracking we'd need position history
                total_loss += abs(deal.profit)

        return {"deals": deal_list, "total_loss_usd": total_loss}
