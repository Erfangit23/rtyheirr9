"""
Backtest module v2 — replays a channel's historical signals against MT5 M1 data.

Fixes over v1:
- No text pre-filter (v1 dropped "GOLD Buy Limit"-style signals entirely).
- Walks back through channel history as far as needed to collect up to 500
  signals (v1 capped at 3000 messages).
- Broker-server time offset auto-detected from a live tick — v1 used the VPS
  machine timezone, so time windows (and therefore results) were wrong whenever
  the VPS clock differed from the broker's server time.
- Signals that filled but never hit TP/SL within the window are reported as
  "expired" (v1 misclassified them as "not filled").
- Applies the same per-channel rules as live trading:
    * per-channel TP index (gold_alicxzos110 TP4, GoldVisionofficial TP3,
      forexkhan TP1, Signal_Atlas TP2, Eliz TP1, others TP2)
    * @Gulljanali17 / @bttesteamin: entry +10 pips toward market, SL +20 pips
    * @BrianTradingForex: dual entry, TP adjustments, TP2 150-pip cap and
      breakeven on the farther leg after TP1
- Estimated USD profit is for NORMAL mode at the base lot (0.01) — 248 is
  intentionally NOT simulated.

The MT5 outcome checks are synchronous; run_backtest yields to the event loop
between signals so the bot stays responsive while a backtest runs.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from signal_parser import parse_signal, Signal

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


@dataclass
class TradeOutcome:
    direction: str
    entry: float
    tp: float
    sl: float
    status: str            # tp_hit / sl_hit / not_filled / cancelled / expired / no_data
    profit_pips: float = 0.0
    rr: float = 0.0
    risk_pips: float = 0.0
    reward_pips: float = 0.0
    entry_time: str = ""
    close_time: str = ""
    signal_time: str = ""
    leg: str = ""          # "1"/"2" for Brian dual-entry legs


@dataclass
class BacktestResult:
    channel: str = ""
    messages_scanned: int = 0
    signals: int = 0
    trades: int = 0
    tp_hit: int = 0
    sl_hit: int = 0
    expired: int = 0
    not_filled: int = 0
    cancelled: int = 0
    no_data: int = 0
    winrate: float = 0.0
    avg_rr: float = 0.0
    gross_profit_pips: float = 0.0
    gross_loss_pips: float = 0.0
    net_pips: float = 0.0
    est_profit_usd: float = 0.0
    first_signal: str = ""
    last_signal: str = ""
    tp_note: str = ""
    results: list = field(default_factory=list)


class Backtester:
    """Backtests historical signals against MT5 M1 price data."""

    TARGET_SIGNALS = 500          # how many signals to collect per run
    MAX_MESSAGES = 50000          # hard cap on messages scanned while walking back
    ENTRY_WINDOW_MIN = 180        # entry must fill within this many minutes
    TP_SL_WINDOW_HOURS = 24       # after fill, wait this long for TP/SL
    PIP = 0.1                     # 1 pip on XAUUSD in price units
    USD_PER_PIP = 0.10            # $ per pip per 0.01 lot on XAUUSD

    def __init__(self, user_client, mt5_connector, logger: Optional[logging.Logger] = None):
        self.user_client = user_client
        self.mt5 = mt5_connector
        self.logger = logger or logging.getLogger("xau_trader")

    # ------------------------------------------------------------------
    # Signal collection
    # ------------------------------------------------------------------
    async def fetch_signals(self, channel_id: str, target: int, fmt: str = "auto"):
        """Collect the most recent `target` parseable signals, walking back
        through channel history as far as needed (bounded by MAX_MESSAGES).

        Returns (signals_chronological, messages_scanned).
        """
        entity = await self.user_client.get_entity(channel_id)
        signals = []
        scanned = 0
        async for msg in self.user_client.iter_messages(entity, limit=self.MAX_MESSAGES):
            scanned += 1
            text = (msg.text or "").strip()
            if len(text) < 5:
                continue
            # No pre-filter here: try the channel's format first, then auto —
            # v1's "must contain XAUUSD/XAU" filter silently dropped channels
            # that write "GOLD Buy Limit ...".
            sig = parse_signal(text, channel_id, fmt) or parse_signal(text, channel_id, "auto")
            if sig:
                signals.append({"signal": sig, "date": msg.date})
                if len(signals) >= target:
                    break

        signals.reverse()  # iter_messages is newest-first -> chronological
        self.logger.info(
            f"Backtest fetch {channel_id}: scanned {scanned} messages, "
            f"found {len(signals)} signals"
        )
        return signals, scanned

    # ------------------------------------------------------------------
    # MT5 time handling
    # ------------------------------------------------------------------
    def _server_offset(self, symbol: str) -> float:
        """Offset (seconds) between broker server time and UTC.

        tick.time is the broker's wall clock expressed as an epoch, so
        offset = tick.time - real epoch. Adding it to a UTC timestamp converts
        it into the broker's time domain, which is what MT5 history calls and
        bar["time"] values use. Falls back to 0 if unavailable.
        """
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick and tick.time:
                return tick.time - time.time()
        except Exception as e:
            self.logger.warning(f"Server offset detection failed: {e}")
        return 0.0

    def _get_rates(self, symbol: str, start_epoch: float, end_epoch: float):
        """Fetch M1 bars for [start_epoch, end_epoch] (broker-time epochs)."""
        if not self.mt5.ensure_connected():
            return None
        frm = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
        to = datetime.fromtimestamp(end_epoch, tz=timezone.utc)
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, frm, to)
        if rates is None or len(rates) == 0:
            count = int((end_epoch - start_epoch) / 60) + 10
            rates = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M1, frm, count)
        return rates if rates is not None and len(rates) > 0 else None

    @staticmethod
    def _fmt_time(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M")

    # ------------------------------------------------------------------
    # Channel rules (mirror live trading)
    # ------------------------------------------------------------------
    TP_RULES = {
        "@gold_alicxzos110": 4,
        "@GoldVisionofficial": 3,
        "@forexkhan": 1,
        "@khanbours": 1,
        "@khanbourse": 1,
        "@khanbouse": 1,
        "@Signal_Atlas": 2,
        "@Eliz_fxac_ademy1": 1,
    }

    def _channel_tp_index(self, channel: str, n_tps: int) -> int:
        idx = self.TP_RULES.get(channel, 2)  # default TP2 like live default
        return max(1, min(idx, n_tps))

    def _apply_channel_adjustments(self, sig: Signal, rates, signal_epoch: float):
        """@Gulljanali17 / @bttesteamin: entry +10 pips toward market (market
        approximated by the first bar close at/after the signal), SL +10 pips
        tighter — same math as live process_signal ($10 SL / $9 TP)."""
        if sig.source_channel not in ("@Gulljanali17", "@bttesteamin"):
            return
        market = None
        for bar in rates:
            if bar["time"] >= signal_epoch:
                market = bar["close"]
                break
        pip = self.PIP
        if market is not None:
            if sig.direction.upper() == "BUY":
                adj = round(sig.entry + 10 * pip, 2)
                if adj < market:
                    sig.entry = adj
            else:
                adj = round(sig.entry - 10 * pip, 2)
                if adj > market:
                    sig.entry = adj
        if sig.direction.upper() == "BUY":
            adj = round(sig.stop_loss + 10 * pip, 2)
            if adj < sig.entry:
                sig.stop_loss = adj
        else:
            adj = round(sig.stop_loss - 10 * pip, 2)
            if adj > sig.entry:
                sig.stop_loss = adj

    def _prep_brian_legs(self, sig: Signal):
        """Build the two Brian legs exactly like live process_signal."""
        e1, e2 = sig.entries[0], sig.entries[1]
        tps = sig.take_profits
        adj = 1.0  # 10 pips
        if sig.direction.upper() == "BUY":
            tp1 = tps[0] - adj
            cap = e2 + (150 * self.PIP) - adj
            tp2 = min(tps[1], cap) if len(tps) >= 2 else cap
            closer, farther = (e1, e2) if e1 >= e2 else (e2, e1)
        else:
            tp1 = tps[0] + adj
            cap = e2 - (150 * self.PIP) + adj
            tp2 = max(tps[1], cap) if len(tps) >= 2 else cap
            closer, farther = (e1, e2) if e1 <= e2 else (e2, e1)
        return (closer, tp1), (farther, tp2)

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    def _simulate_single(self, direction, entry, tp, sl, rates, signal_epoch) -> TradeOutcome:
        entry_window = self.ENTRY_WINDOW_MIN * 60
        tp_window = self.TP_SL_WINDOW_HOURS * 3600
        risk = abs(entry - sl) / self.PIP
        reward = abs(tp - entry) / self.PIP
        out = TradeOutcome(
            direction=direction, entry=entry, tp=tp, sl=sl,
            status="not_filled", risk_pips=risk, reward_pips=reward,
            rr=(reward / risk) if risk else 0.0,
        )
        filled_epoch = None
        for bar in rates:
            t = bar["time"]
            if t < signal_epoch:
                continue
            if filled_epoch is None:
                if t > signal_epoch + entry_window:
                    break
                if direction == "SELL":
                    if bar["high"] >= entry:
                        filled_epoch = t
                elif bar["low"] <= entry:
                    filled_epoch = t
                if filled_epoch is None:
                    continue
                out.entry_time = self._fmt_time(filled_epoch)
            # From the fill bar onward; SL checked first (conservative)
            if t > filled_epoch + tp_window:
                out.status = "expired"
                out.close_time = self._fmt_time(t)
                return out
            if direction == "SELL":
                if bar["high"] >= sl:
                    out.status, out.profit_pips = "sl_hit", -risk
                    out.close_time = self._fmt_time(t)
                    return out
                if bar["low"] <= tp:
                    out.status, out.profit_pips = "tp_hit", reward
                    out.close_time = self._fmt_time(t)
                    return out
            else:
                if bar["low"] <= sl:
                    out.status, out.profit_pips = "sl_hit", -risk
                    out.close_time = self._fmt_time(t)
                    return out
                if bar["high"] >= tp:
                    out.status, out.profit_pips = "tp_hit", reward
                    out.close_time = self._fmt_time(t)
                    return out
        if filled_epoch is not None:
            out.status = "expired"
        return out

    def _simulate_brian(self, sig, rates, signal_epoch) -> list:
        """Dual-entry simulation: closer leg -> TP1, farther leg -> TP2 with
        breakeven after the closer leg hits TP1. Both entries are pulled 5 pips
        toward market (first bar close at/after the signal approximates it).
        Cancel rule mirrors live: if the channel's raw TP2 is reached before the
        closer leg fills, both legs are cancelled."""
        direction = sig.direction.upper()

        # Pull entries 5 pips toward market (same as live process_signal)
        market = None
        for bar in rates:
            if bar["time"] >= signal_epoch:
                market = bar["close"]
                break
        pull = 5 * self.PIP
        adj_entries = []
        for e in sig.entries:
            a = round(e + pull, 2) if direction == "BUY" else round(e - pull, 2)
            if market is None or (a < market if direction == "BUY" else a > market):
                adj_entries.append(a)
            else:
                adj_entries.append(e)
        sig.entries = adj_entries

        (e1, tp1), (e2, tp2) = self._prep_brian_legs(sig)
        sl_orig = sig.stop_loss
        # Cancel level: the channel's raw TP2 (NOT the adjusted leg TPs)
        cancel_tp = sig.take_profits[1] if len(sig.take_profits) >= 2 else tp1
        entry_window = self.ENTRY_WINDOW_MIN * 60
        tp_window = self.TP_SL_WINDOW_HOURS * 3600

        legs = [
            {"e": e1, "tp": tp1, "sl": sl_orig, "fill": None, "closed": False,
             "status": "not_filled", "close": None, "tag": "1"},
            {"e": e2, "tp": tp2, "sl": sl_orig, "fill": None, "closed": False,
             "status": "not_filled", "close": None, "tag": "2"},
        ]

        for bar in rates:
            t = bar["time"]
            if t < signal_epoch:
                continue
            hi, lo = bar["high"], bar["low"]

            # Cancel rule: price reached the channel TP2 while the closer leg
            # never filled -> both cancelled (live behavior)
            if legs[0]["fill"] is None:
                if t > signal_epoch + entry_window:
                    break
                if (direction == "SELL" and lo <= cancel_tp) or (direction == "BUY" and hi >= cancel_tp):
                    legs[0]["status"] = legs[1]["status"] = "cancelled"
                    legs[0]["closed"] = legs[1]["closed"] = True
                    legs[0]["close"] = legs[1]["close"] = t
                    break

            # Fills (limit orders)
            for L in legs:
                if L["fill"] is None and (
                    (direction == "SELL" and hi >= L["e"]) or
                    (direction == "BUY" and lo <= L["e"])
                ):
                    L["fill"] = t

            # Closes (SL first — conservative)
            for L in legs:
                if L["fill"] is not None and not L["closed"]:
                    if t > L["fill"] + tp_window:
                        L["status"], L["closed"], L["close"] = "expired", True, t
                        continue
                    if direction == "SELL":
                        if hi >= L["sl"]:
                            L["status"], L["closed"], L["close"] = "sl_hit", True, t
                        elif lo <= L["tp"]:
                            L["status"], L["closed"], L["close"] = "tp_hit", True, t
                    else:
                        if lo <= L["sl"]:
                            L["status"], L["closed"], L["close"] = "sl_hit", True, t
                        elif hi >= L["tp"]:
                            L["status"], L["closed"], L["close"] = "tp_hit", True, t

            # Breakeven: closer leg hit TP1 -> farther leg SL to its entry
            if legs[0]["status"] == "tp_hit" and abs(legs[1]["sl"] - e2) > 1e-9:
                if not legs[1]["closed"]:
                    legs[1]["sl"] = e2

            if all(L["closed"] for L in legs):
                break

        results = []
        for L in legs:
            risk = abs(L["e"] - L["sl"]) / self.PIP if L["fill"] is not None else abs(L["e"] - sl_orig) / self.PIP
            reward = abs(L["tp"] - L["e"]) / self.PIP
            profit = 0.0
            if L["status"] == "tp_hit":
                profit = reward
            elif L["status"] == "sl_hit":
                profit = -(abs(L["e"] - L["sl"]) / self.PIP)
            results.append(TradeOutcome(
                direction=direction, entry=L["e"], tp=L["tp"], sl=L["sl"],
                status=L["status"], profit_pips=profit,
                risk_pips=risk, reward_pips=reward,
                rr=(reward / risk) if risk else 0.0,
                entry_time=self._fmt_time(L["fill"]) if L["fill"] else "",
                close_time=self._fmt_time(L["close"]) if L["close"] else "",
                leg=L["tag"],
            ))
        return results

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    async def run_backtest(self, channel_id: str, fmt: str = "auto",
                           target_signals: Optional[int] = None) -> BacktestResult:
        target = target_signals or self.TARGET_SIGNALS
        result = BacktestResult(channel=channel_id, tp_note=self._tp_note(channel_id))

        signals, scanned = await self.fetch_signals(channel_id, target, fmt)
        result.messages_scanned = scanned
        result.signals = len(signals)
        if not signals:
            return result

        result.first_signal = signals[0]["date"].strftime("%Y-%m-%d")
        result.last_signal = signals[-1]["date"].strftime("%Y-%m-%d")

        offset = self._server_offset(signals[0]["signal"].symbol)
        if offset:
            self.logger.info(f"Backtest: broker server offset {offset/3600:+.1f}h from UTC")

        for item in signals:
            await asyncio.sleep(0)  # keep the bot responsive between signals
            sig = item["signal"]
            signal_epoch = item["date"].timestamp() + offset
            out_tp = self._channel_tp_index(sig.source_channel, len(sig.take_profits))

            end_epoch = signal_epoch + (self.ENTRY_WINDOW_MIN * 60 + self.TP_SL_WINDOW_HOURS * 3600) + 300
            rates = self._get_rates(sig.symbol, signal_epoch - 300, end_epoch)

            stamp = item["date"].strftime("%m-%d %H:%M")
            if rates is None:
                result.no_data += 1
                result.trades += 1
                result.results.append(TradeOutcome(
                    direction=sig.direction, entry=sig.entry, tp=0.0,
                    sl=sig.stop_loss, status="no_data", signal_time=stamp))
                continue

            self._apply_channel_adjustments(sig, rates, signal_epoch)

            is_brian = (sig.source_channel == "@BrianTradingForex"
                        and len(getattr(sig, "entries", []) or []) >= 2
                        and len(sig.take_profits) >= 2)
            if is_brian:
                outcomes = self._simulate_brian(sig, rates, signal_epoch)
            else:
                outcomes = [self._simulate_single(
                    sig.direction.upper(), sig.entry,
                    sig.take_profits[out_tp - 1], sig.stop_loss,
                    rates, signal_epoch)]

            for o in outcomes:
                o.signal_time = stamp
                result.results.append(o)
                result.trades += 1
                if o.status == "tp_hit":
                    result.tp_hit += 1
                    result.gross_profit_pips += o.profit_pips
                elif o.status == "sl_hit":
                    result.sl_hit += 1
                    result.gross_loss_pips += abs(o.profit_pips)
                elif o.status == "expired":
                    result.expired += 1
                elif o.status == "not_filled":
                    result.not_filled += 1
                elif o.status == "cancelled":
                    result.cancelled += 1

        closed = result.tp_hit + result.sl_hit
        if closed:
            result.winrate = result.tp_hit / closed * 100
        result.net_pips = result.gross_profit_pips - result.gross_loss_pips
        result.est_profit_usd = result.net_pips * self.USD_PER_PIP
        rr_list = [o.rr for o in result.results if o.rr > 0]
        if rr_list:
            result.avg_rr = sum(rr_list) / len(rr_list)
        return result

    def _tp_note(self, channel_id: str) -> str:
        if channel_id == "@BrianTradingForex":
            return "dual entry: entries +5p closer, closer->TP1(-10p), farther->TP2(cap 150p, breakeven), cancel at TP2 unfilled"
        if channel_id in ("@Gulljanali17", "@bttesteamin"):
            return f"TP2, entry +10p, SL +20p (live rules)"
        return f"TP{self.TP_RULES.get(channel_id, 2)} (live rules)"

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------
    def format_results(self, result: BacktestResult) -> str:
        filled = result.tp_hit + result.sl_hit + result.expired
        fill_rate = (filled / result.trades * 100) if result.trades else 0
        sign = "+" if result.est_profit_usd >= 0 else ""
        win_sign = "+" if result.net_pips >= 0 else ""

        lines = [
            f"📊 Backtest: {result.channel}",
            f"Period: {result.first_signal or '—'} → {result.last_signal or '—'} (UTC)",
            f"Messages scanned: {result.messages_scanned} | Signals: {result.signals}",
            f"Rules: {result.tp_note}",
            "",
            f"Trades: {result.trades} | Filled: {filled} ({fill_rate:.0f}%)",
            f"  ✅ TP: {result.tp_hit} | ❌ SL: {result.sl_hit} | ⏳ Expired: {result.expired}",
            f"  ⚪ Not filled: {result.not_filled} | 🗑️ Cancelled before fill: {result.cancelled} | ⚠️ No data: {result.no_data}",
            "",
            f"🎯 Winrate: {result.winrate:.1f}% ({result.tp_hit}W / {result.sl_hit}L)",
            f"📊 Avg RR: 1:{result.avg_rr:.2f}",
            f"💰 Net: {win_sign}{result.net_pips:.0f} pips "
            f"(profit +{result.gross_profit_pips:.0f} / loss -{result.gross_loss_pips:.0f})",
            f"💵 Est. profit @ 0.01 lot: {sign}${result.est_profit_usd:.2f} (normal mode, no 248)",
        ]

        recent = result.results[-15:]
        if recent:
            lines.append("\n--- Last 15 trades ---")
            for r in recent:
                if r.status == "tp_hit":
                    icon = "✅"
                    detail = f"TP +{r.reward_pips:.0f}p"
                elif r.status == "sl_hit":
                    icon = "❌"
                    detail = f"SL -{r.risk_pips:.0f}p"
                elif r.status == "expired":
                    icon = "⏳"
                    detail = "expired open"
                elif r.status == "cancelled":
                    icon = "🗑️"
                    detail = "cancelled (TP1 before fill)"
                elif r.status == "no_data":
                    icon = "⚠️"
                    detail = "no price data"
                else:
                    icon = "⚪"
                    detail = "not filled"
                leg = f" L{r.leg}" if r.leg else ""
                lines.append(
                    f"{icon} {r.signal_time} {r.direction}{leg} E={r.entry:g} RR=1:{r.rr:.1f} -> {detail}"
                )

        text = "\n".join(lines)
        return text[:4000] + "\n…" if len(text) > 4096 else text
