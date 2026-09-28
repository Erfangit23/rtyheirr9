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
- Estimated USD profit is for the base lot (0.01).

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
from signal_validator import ema_value, rsi_wilder, atr_wilder

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
    filter_blocked: bool = False  # filter simulation: signal would be blocked
    filter_reasons: str = ""      # failed filter names, ", "-joined
    m_ema_atr: float = None       # (price - EMA200 H1) in ATR units at signal time
    m_rsi: float = None           # RSI(14, M15) at signal time
    m_sl_atr: float = None        # |entry - SL| in ATR(M15) units


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
    rejected: int = 0        # signals live trading would refuse (wrong side / SL cap)
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
    # --- filter simulation (EMA200/RSI/ATR evaluated at signal time) ---
    filter_sim: bool = False         # simulation ran (settings provided)
    filter_blocked: int = 0          # trades the filters would have blocked
    filter_blocked_tp: int = 0      # ...of those, how many hit TP (lost profit)
    filter_blocked_sl: int = 0      # ...of those, how many hit SL (avoided loss)
    filter_blocked_net: float = 0.0 # net pips of the blocked trades (neg = good to block)
    filter_reason_counts: dict = field(default_factory=dict)  # filter name -> block count
    kept_tp: int = 0                 # trades kept by the filters
    kept_sl: int = 0
    kept_net: float = 0.0            # net pips with filters ON


class Backtester:
    """Backtests historical signals against MT5 M1 price data."""

    TARGET_SIGNALS = 500          # how many signals to collect per run
    MAX_MESSAGES = 50000          # hard cap on messages scanned while walking back
    ENTRY_WINDOW_MIN = 180        # entry must fill within this many minutes
    TP_SL_WINDOW_HOURS = 24       # after fill, wait this long for TP/SL
    PIP = 0.1                     # 1 pip on XAUUSD in price units
    USD_PER_PIP = 0.10            # $ per pip per 0.01 lot on XAUUSD

    def __init__(self, user_client, mt5_connector, logger: Optional[logging.Logger] = None,
                 settings=None):
        self.user_client = user_client
        self.mt5 = mt5_connector
        self.logger = logger or logging.getLogger("xau_trader")
        self.settings = settings  # enables the filter simulation when present

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

    # khan family: TP1 target + cancel the pending order if TP1 is reached unfilled
    KHAN_CHANNELS = ("@forexkhan", "@khanbours", "@khanbourse", "@khanbouse")

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
    # Filter simulation (EMA200 / RSI / ATR evaluated at signal time)
    # ------------------------------------------------------------------
    TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
                  "H1": 3600, "H4": 14400, "D1": 86400}
    # Bars fetched before the signal. NOTE: the fetch is TIME based, and gold
    # only trades ~5 days a week — so a window of N calendar hours yields only
    # ~N*5/7 bars. EMA200 needs 201 COMPLETED H1 bars, i.e. ~285 calendar
    # hours; 400 gives ~285 trading bars and a safe margin.
    FILTER_BARS = {"H1": 400, "M15": 400}

    def _completed_bars(self, symbol: str, tf_name: str, signal_epoch: float):
        """Bars of tf_name that fully closed before signal_epoch (oldest->newest),
        or None. Mirrors the live validator (which drops the forming bar)."""
        try:
            if mt5 is None:
                return None
            tf = getattr(mt5, f"TIMEFRAME_{tf_name}", None)
            if tf is None:
                return None
            secs = self.TF_SECONDS[tf_name]
            count = self.FILTER_BARS.get(tf_name, 260)
            to = datetime.fromtimestamp(signal_epoch, tz=timezone.utc)
            frm = datetime.fromtimestamp(signal_epoch - (count + 5) * secs, tz=timezone.utc)
            rates = mt5.copy_rates_range(symbol, tf, frm, to)
            if rates is None or len(rates) == 0:
                return None
            done = [r for r in rates if r["time"] + secs <= signal_epoch]
            if len(done) < 30:
                return None
            return done
        except Exception as e:
            self.logger.warning(f"Filter-sim bar fetch failed ({symbol} {tf_name}): {e}")
            return None

    def _filter_failures(self, sig: Signal, signal_epoch: float):
        """Evaluate the live signal filters AS OF the signal time.

        Returns (failures, metrics):
          failures: None when simulation is unavailable (no settings / channel
                    has filters off entirely), else a list of (name, detail).
          metrics : the raw indicator values, kept so the report can re-score
                    them against other thresholds.

        Every check FAILS OPEN on missing data, exactly like live trading —
        a data gap never counts as a block.
        """
        if not self.settings:
            return None, {}
        try:
            if self.settings.channel_filters_disabled(sig.source_channel):
                return [], {}
        except Exception:
            return None, {}

        m = self._filter_metrics(sig, signal_epoch)
        fails = []
        for fname, thr_key in (("ema200", "buffer_atr_mult"),
                               ("rsi", None),
                               ("atr_sl", "min_sl_atr_mult")):
            try:
                cfg = self.settings.filter_config_for_channel(sig.source_channel, fname)
            except Exception:
                cfg = None
            if not cfg or not cfg.get("enabled", True):
                continue
            if fname == "rsi":
                thr = (cfg.get("buy_max", 75), cfg.get("sell_min", 25))
            else:
                thr = cfg.get(thr_key, 0.3 if fname == "ema200" else 0.5)
            detail = self._metric_failure(fname, thr, sig, m)
            if detail:
                fails.append((fname, detail))
        return fails, m

    def _filter_metrics(self, sig: Signal, signal_epoch: float) -> dict:
        """Raw indicator values at signal time (None when data is missing).

        ema_atr : (price - EMA200 H1) in ATR(H1) units, signed
        rsi     : RSI(14, M15)
        sl_atr  : |entry - SL| in ATR(M15) units

        Computing the metrics once lets the same numbers be re-scored against
        many candidate thresholds (see _scan_table) without re-fetching data.
        """
        m = {"ema_atr": None, "rsi": None, "sl_atr": None}

        bars = self._completed_bars(sig.symbol, "H1", signal_epoch)
        if bars is not None and len(bars) >= 201:
            closes = [float(r["close"]) for r in bars]
            ema = ema_value(closes, 200)
            atr = atr_wilder([float(r["high"]) for r in bars],
                             [float(r["low"]) for r in bars], closes, 14)
            if ema is not None:
                price = closes[-1]
                atr_val = atr if atr and atr > 0 else abs(price) * 0.001
                m["ema_atr"] = (price - ema) / atr_val

        bars15 = self._completed_bars(sig.symbol, "M15", signal_epoch)
        if bars15 is not None:
            closes15 = [float(r["close"]) for r in bars15]
            rsi = rsi_wilder(closes15, 14)
            if rsi is not None:
                m["rsi"] = rsi
            atr15 = atr_wilder([float(r["high"]) for r in bars15],
                               [float(r["low"]) for r in bars15], closes15, 14)
            if atr15 and atr15 > 0:
                m["sl_atr"] = abs(float(sig.entry) - float(sig.stop_loss)) / atr15
        return m

    def _metric_failure(self, fname: str, thresholds, sig: Signal, m: dict):
        """Apply one filter's thresholds to pre-computed metrics.

        thresholds: for ema200 a float buffer (ATR units), for rsi a
        (buy_max, sell_min) tuple, for atr_sl a float floor (ATR units).
        Returns a failure detail string, or None when the check passes
        (including when the metric is unavailable — fail open, like live).
        """
        d = sig.direction.upper()
        if fname == "ema200":
            v = m.get("ema_atr")
            if v is None:
                return None
            if d == "BUY" and v < -float(thresholds):
                return f"price {abs(v):.2f}xATR below EMA200"
            if d == "SELL" and v > float(thresholds):
                return f"price {v:.2f}xATR above EMA200"
            return None
        if fname == "rsi":
            v = m.get("rsi")
            if v is None:
                return None
            buy_max, sell_min = thresholds
            if d == "BUY" and v >= float(buy_max):
                return f"RSI {v:.1f} >= {float(buy_max):g}"
            if d == "SELL" and v <= float(sell_min):
                return f"RSI {v:.1f} <= {float(sell_min):g}"
            return None
        if fname == "atr_sl":
            v = m.get("sl_atr")
            if v is None:
                return None
            if v < float(thresholds):
                return f"SL {v:.2f}xATR inside floor {float(thresholds):g}x"
            return None
        return None

    # ------------------------------------------------------------------
    # Threshold scan — which setting would actually have helped?
    # ------------------------------------------------------------------
    METRIC_ATTR = {"ema200": "m_ema_atr", "rsi": "m_rsi", "atr_sl": "m_sl_atr"}
    # A deliberately strict combination, shown in every scan so you can see
    # what "much stricter" would have done before applying it.
    STRICT_PRESET = {"ema200": 0.0, "rsi": (60, 40), "atr_sl": 0.8}
    SCAN_MIN_TRADES = 10     # don't draw conclusions from tiny samples
    SCAN_MAX_BLOCK_PCT = 0.5  # a "filter" that blocks most trades is a shutdown,
                              # not a filter — such thresholds are ignored
    SCAN_EMA_BUFFER = [0.0, 0.2, 0.3, 0.5, 1.0, 2.0]
    SCAN_RSI = [(50, 50), (55, 45), (60, 40), (65, 35), (70, 30), (75, 25)]
    SCAN_ATR_FLOOR = [0.2, 0.3, 0.5, 0.8, 1.0, 1.5, 2.0]

    def _scan_eval(self, closed, base_net, fname, thr):
        """Replay the already-simulated trades against one candidate threshold."""
        blocked = [o for o in closed
                   if self._metric_failure(fname, thr, o,
                                           {"ema_atr": o.m_ema_atr,
                                            "rsi": o.m_rsi,
                                            "sl_atr": o.m_sl_atr})]
        blocked_ids = {id(o) for o in blocked}
        kept = [o for o in closed if id(o) not in blocked_ids]
        k_tp = sum(1 for o in kept if o.status == "tp_hit")
        k_net = sum(o.profit_pips for o in kept)
        b_tp = sum(1 for o in blocked if o.status == "tp_hit")
        return {
            "thr": thr,
            "blocked": len(blocked),
            "b_tp": b_tp,
            "b_sl": len(blocked) - b_tp,
            "kept_net": k_net,
            "delta": k_net - base_net,
            "kept_ids": {id(o) for o in kept},
        }

    def _scan_table(self, result) -> list:
        """Show, per filter, the current threshold and the best one.

        The SAME simulated trades are re-scored against candidate thresholds,
        so the numbers are directly comparable — this is how to pick a
        stricter setting that actually separates losers from winners instead
        of just blocking more trades.
        """
        closed = [o for o in result.results if o.status in ("tp_hit", "sl_hit")]
        if len(closed) < self.SCAN_MIN_TRADES or not self.settings:
            return []
        base_net = sum(o.profit_pips for o in closed)
        base_tp = sum(1 for o in closed if o.status == "tp_hit")

        def fmt_row(label, ev, current=False):
            mark = " ←current" if current else ""
            verdict = "✅" if ev["delta"] > 0 else ("➖" if ev["delta"] == 0 else "⚠️")
            return (f"  {label}: blocks {ev['blocked']} ({ev['b_tp']}W/{ev['b_sl']}L) "
                    f"net {base_net:+.0f}→{ev['kept_net']:+.0f} ({ev['delta']:+.0f}) "
                    f"{verdict}{mark}")

        lines = []
        best = {}   # fname -> (label, ev)
        ch = result.channel

        specs = (
            ("ema200", "buffer_atr_mult", self.SCAN_EMA_BUFFER,
             lambda t: f"EMA200 buffer {t:g}x", 0.3),
            ("rsi", None, self.SCAN_RSI,
             lambda t: f"RSI {t[0]:g}/{t[1]:g}", (75, 25)),
            ("atr_sl", "min_sl_atr_mult", self.SCAN_ATR_FLOOR,
             lambda t: f"ATR floor {t:g}x", 0.5),
        )
        for fname, key, candidates, labeler, default in specs:
            try:
                cfg = self.settings.filter_config_for_channel(ch, fname)
            except Exception:
                cfg = None
            if not cfg or not cfg.get("enabled", True):
                continue  # filter off for this channel — nothing to scan

            current = ((cfg.get("buy_max", 75), cfg.get("sell_min", 25))
                       if fname == "rsi" else cfg.get(key, default))

            evs = [(t, self._scan_eval(closed, base_net, fname, t)) for t in candidates]
            max_block = len(closed) * self.SCAN_MAX_BLOCK_PCT
            # keep only thresholds that actually block something without
            # degenerating into "block everything"
            evs = [e for e in evs if 0 < e[1]["blocked"] <= max_block]
            if not evs:
                # Never let a filter disappear silently: say whether it had no
                # data (fail-open) or simply never triggered.
                has_data = any(getattr(o, self.METRIC_ATTR[fname]) is not None
                               for o in closed)
                if not has_data:
                    lines.append(f"  {labeler(default)}: ⚠️ NO DATA at signal time "
                                 f"(check the H1/M15 history window) — filter inactive")
                else:
                    lines.append(f"  {labeler(default)}: blocks 0 — nothing to "
                                 f"filter, every signal already passes")
                continue
            best_t, best_ev = max(evs, key=lambda e: (e[1]["delta"], -e[1]["blocked"]))

            cur_ev = self._scan_eval(closed, base_net, fname, current)
            cur_label = labeler(current)
            best_label = labeler(best_t)
            if best_label == cur_label or best_ev["delta"] <= 0:
                # nothing beats the current setting
                lines.append(fmt_row(cur_label, cur_ev, current=True))
            else:
                lines.append(fmt_row(cur_label, cur_ev, current=True))
                lines.append(fmt_row(best_label, best_ev))
                best[fname] = (best_t, best_ev, best_label)

        if not lines:
            return []

        out = ["", "📐 Threshold scan (same trades, different thresholds):"]
        out += lines

        # what a deliberately strict preset would have done
        if self.settings:
            keep_ids, blocked_any = None, False
            for fname, thr in (("ema200", self.STRICT_PRESET["ema200"]),
                               ("rsi", self.STRICT_PRESET["rsi"]),
                               ("atr_sl", self.STRICT_PRESET["atr_sl"])):
                try:
                    cfg = self.settings.filter_config_for_channel(ch, fname)
                except Exception:
                    cfg = None
                if not cfg or not cfg.get("enabled", True):
                    continue
                ev = self._scan_eval(closed, base_net, fname, thr)
                keep_ids = ev["kept_ids"] if keep_ids is None else (keep_ids & ev["kept_ids"])
                blocked_any = blocked_any or ev["blocked"] > 0
            if keep_ids is not None and blocked_any:
                kept = [o for o in closed if id(o) in keep_ids]
                k_tp = sum(1 for o in kept if o.status == "tp_hit")
                k_net = sum(o.profit_pips for o in kept)
                blocked = len(closed) - len(kept)
                b_tp = base_tp - k_tp
                delta = k_net - base_net
                v = "✅ HELPS" if delta > 0 else ("➖ neutral" if delta == 0 else "⚠️ HURTS")
                out.append(
                    f"  Strict preset (EMA 0x + RSI 60/40 + ATR 0.8x): blocks {blocked} "
                    f"({b_tp}W/{blocked - b_tp}L), net {base_net:+.0f}→{k_net:+.0f} "
                    f"({delta:+.0f}) {v}"
                )
                out.append("  One-shot: /filterpreset strict  (or /filterpreset balanced)")

        # combined effect of the best thresholds
        if best:
            keep_ids = None
            for fname, (t, ev, _lbl) in best.items():
                ids = ev["kept_ids"]
                keep_ids = ids if keep_ids is None else (keep_ids & ids)
            kept = [o for o in closed if id(o) in (keep_ids or set())]
            k_tp = sum(1 for o in kept if o.status == "tp_hit")
            k_sl = len(kept) - k_tp
            k_net = sum(o.profit_pips for o in kept)
            blocked = len(closed) - len(kept)
            b_tp = base_tp - k_tp
            delta = k_net - base_net
            combo = " + ".join(f"{lbl}" for _t, _ev, lbl in best.values())
            verdict = "✅ HELPS" if delta > 0 else ("➖ neutral" if delta == 0 else "⚠️ HURTS")
            out.append(
                f"  🏆 Best combo ({combo}): blocks {blocked} ({b_tp}W/{blocked - b_tp}L), "
                f"net {base_net:+.0f}→{k_net:+.0f} ({delta:+.0f}) {verdict}"
            )
            out.append("  Apply with /femabuf, /rsith, /atrfloor — or /filtermode on to enforce.")
        return out

    # ------------------------------------------------------------------
    # Live-rule sanity (mirrors TradeManager.process_signal)
    # ------------------------------------------------------------------
    MAX_SL_PIPS = 150  # live default max_sl_pips

    def _sanity_reject(self, sig: Signal, enforce_sl_cap: bool = True):
        """Return a reason when live trading would REFUSE this signal, else None.

        Live rejects SL/TP on the wrong side of the entry, and (except for the
        Brian dual-entry strategy, which is a breakeven play) an SL wider than
        the cap. The backtest used to simulate those signals anyway — and since
        a wrong-side TP is touched on the very first bar, they were counted as
        instant wins worth tens of thousands of pips, wrecking the totals.
        """
        entry = float(sig.entry)
        d = sig.direction.upper()
        if d == "BUY":
            if sig.stop_loss >= entry:
                return "SL on the wrong side for BUY"
            if any(tp <= entry for tp in sig.take_profits):
                return "TP on the wrong side for BUY"
        elif d == "SELL":
            if sig.stop_loss <= entry:
                return "SL on the wrong side for SELL"
            if any(tp >= entry for tp in sig.take_profits):
                return "TP on the wrong side for SELL"
        if enforce_sl_cap:
            sl_pips = abs(entry - sig.stop_loss) / self.PIP
            if sl_pips > self.MAX_SL_PIPS:
                return f"SL {sl_pips:.0f} pips > {self.MAX_SL_PIPS} cap"
        return None

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------
    def _simulate_single(self, direction, entry, tp, sl, rates, signal_epoch,
                         cancel_at=None) -> TradeOutcome:
        """cancel_at: live cancel rule — if price reaches this level while the
        order is STILL unfilled, the pending order is cancelled (khan channels)."""
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
                    # Still unfilled: the live rule for khan channels cancels the
                    # pending order once price reaches TP1 without filling.
                    if cancel_at is not None and (
                        (direction == "SELL" and bar["low"] <= cancel_at) or
                        (direction == "BUY" and bar["high"] >= cancel_at)
                    ):
                        out.status = "cancelled"
                        out.close_time = self._fmt_time(t)
                        return out
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

            # Filter simulation: evaluate on the RAW signal, before channel
            # adjustments — exactly where the live validator runs. A signal is
            # "blocked" if any enabled filter would have rejected it.
            flt_fails, flt_metrics = self._filter_failures(sig, signal_epoch)
            if flt_fails is not None:
                result.filter_sim = True
            flt_reasons = ", ".join(n for n, _ in flt_fails) if flt_fails else ""
            flt_blocked = bool(flt_fails)

            end_epoch = signal_epoch + (self.ENTRY_WINDOW_MIN * 60 + self.TP_SL_WINDOW_HOURS * 3600) + 300
            rates = self._get_rates(sig.symbol, signal_epoch - 300, end_epoch)

            stamp = item["date"].strftime("%m-%d %H:%M")
            if rates is None:
                result.no_data += 1
                result.trades += 1
                result.results.append(TradeOutcome(
                    direction=sig.direction, entry=sig.entry, tp=0.0,
                    sl=sig.stop_loss, status="no_data", signal_time=stamp,
                    filter_blocked=flt_blocked, filter_reasons=flt_reasons,
                    m_ema_atr=flt_metrics.get("ema_atr"),
                    m_rsi=flt_metrics.get("rsi"),
                    m_sl_atr=flt_metrics.get("sl_atr")))
                continue

            self._apply_channel_adjustments(sig, rates, signal_epoch)

            is_brian = (sig.source_channel == "@BrianTradingForex"
                        and len(getattr(sig, "entries", []) or []) >= 2
                        and len(sig.take_profits) >= 2)

            # Live refuses these outright — simulating them would book fantasy
            # trades (a wrong-side TP is "touched" on the first bar).
            reject_reason = self._sanity_reject(sig, enforce_sl_cap=not is_brian)
            if reject_reason:
                result.rejected += 1
                result.results.append(TradeOutcome(
                    direction=sig.direction, entry=sig.entry, tp=0.0,
                    sl=sig.stop_loss, status="rejected", signal_time=stamp,
                    filter_blocked=flt_blocked, filter_reasons=flt_reasons,
                    m_ema_atr=flt_metrics.get("ema_atr"),
                    m_rsi=flt_metrics.get("rsi"),
                    m_sl_atr=flt_metrics.get("sl_atr")))
                self.logger.debug(
                    f"Backtest: {sig.source_channel} signal rejected ({reject_reason})"
                )
                continue

            if is_brian:
                outcomes = self._simulate_brian(sig, rates, signal_epoch)
            else:
                # khan channels cancel the pending order if TP1 is reached unfilled
                cancel_at = (sig.take_profits[0]
                             if sig.source_channel in self.KHAN_CHANNELS else None)
                outcomes = [self._simulate_single(
                    sig.direction.upper(), sig.entry,
                    sig.take_profits[out_tp - 1], sig.stop_loss,
                    rates, signal_epoch, cancel_at=cancel_at)]

            for o in outcomes:
                o.signal_time = stamp
                o.filter_blocked = flt_blocked
                o.filter_reasons = flt_reasons
                o.m_ema_atr = flt_metrics.get("ema_atr")
                o.m_rsi = flt_metrics.get("rsi")
                o.m_sl_atr = flt_metrics.get("sl_atr")
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

        # --- Filter-simulation aggregation ---
        if result.filter_sim:
            for o in result.results:
                if o.status == "rejected":
                    continue  # live would refuse these regardless of filters
                if o.filter_blocked:
                    result.filter_blocked += 1
                    if o.status == "tp_hit":
                        result.filter_blocked_tp += 1
                        result.filter_blocked_net += o.profit_pips
                    elif o.status == "sl_hit":
                        result.filter_blocked_sl += 1
                        result.filter_blocked_net += o.profit_pips
                    for rn in (r.strip() for r in o.filter_reasons.split(",")):
                        if rn:
                            result.filter_reason_counts[rn] = (
                                result.filter_reason_counts.get(rn, 0) + 1
                            )
                else:
                    if o.status == "tp_hit":
                        result.kept_tp += 1
                        result.kept_net += o.profit_pips
                    elif o.status == "sl_hit":
                        result.kept_sl += 1
                        result.kept_net += o.profit_pips
        return result

    def _tp_note(self, channel_id: str) -> str:
        if channel_id in self.KHAN_CHANNELS:
            return "TP1, cancel if TP1 reached unfilled (live rules)"
        if channel_id == "@BrianTradingForex":
            return "dual entry: entries +5p closer, closer->TP1(-10p), farther->TP2(cap 150p, breakeven), cancel at TP2 unfilled"
        if channel_id in ("@Gulljanali17", "@bttesteamin"):
            return "TP2, entry +10p, SL +10p tighter (live rules)"
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
            f"  ⚪ Not filled: {result.not_filled} | 🗑️ Cancelled before fill: {result.cancelled}",
            f"  🚫 Rejected by live rules (wrong side / SL cap): {result.rejected}"
            f" | ⚠️ No data: {result.no_data}",
            "",
            f"🎯 Winrate: {result.winrate:.1f}% ({result.tp_hit}W / {result.sl_hit}L)",
            f"📊 Avg RR: 1:{result.avg_rr:.2f}",
            f"💰 Net: {win_sign}{result.net_pips:.0f} pips "
            f"(profit +{result.gross_profit_pips:.0f} / loss -{result.gross_loss_pips:.0f})",
            f"💵 Est. profit @ 0.01 lot: {sign}${result.est_profit_usd:.2f}",
        ]

        # --- Filter-simulation section ---
        if result.filter_sim:
            pct = (result.filter_blocked / result.trades * 100) if result.trades else 0.0
            kept_closed = result.kept_tp + result.kept_sl
            kept_wr = (result.kept_tp / kept_closed * 100) if kept_closed else 0.0
            b_sign = "+" if result.filter_blocked_net >= 0 else ""
            k_sign = "+" if result.kept_net >= 0 else ""
            reasons = ", ".join(
                f"{k} {v}"
                for k, v in sorted(result.filter_reason_counts.items(), key=lambda kv: -kv[1])
            )
            if result.filter_blocked_net < 0:
                verdict = (f"✅ Filters HELP this channel: the blocked trades lost "
                           f"{abs(result.filter_blocked_net):.0f} pips overall")
            elif result.filter_blocked_net > 0:
                verdict = (f"⚠️ Filters HURT this channel: the blocked trades were "
                           f"net profitable ({result.filter_blocked_net:+.0f} pips)")
            else:
                verdict = "➖ Filters neutral here: blocked trades net 0 pips"
            lines += [
                "",
                "🧪 Filter simulation (EMA200/RSI/ATR evaluated at signal time):",
                f"Would block {result.filter_blocked}/{result.trades} trades ({pct:.0f}%)",
                f"  Blocked: {result.filter_blocked_tp} TP / {result.filter_blocked_sl} SL "
                f"— net {b_sign}{result.filter_blocked_net:.0f} pips",
                f"  With filters ON: winrate {result.winrate:.1f}% → {kept_wr:.1f}%, "
                f"net {win_sign}{result.net_pips:.0f} → {k_sign}{result.kept_net:.0f} pips",
                f"  Reasons: {reasons or 'none'}",
                f"  {verdict}",
            ]
            lines += self._scan_table(result)

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
                elif r.status == "rejected":
                    icon = "🚫"
                    detail = "rejected (live rules)"
                elif r.status == "no_data":
                    icon = "⚠️"
                    detail = "no price data"
                else:
                    icon = "⚪"
                    detail = "not filled"
                leg = f" L{r.leg}" if r.leg else ""
                blk = " 🧪filtered" if r.filter_blocked else ""
                lines.append(
                    f"{icon} {r.signal_time} {r.direction}{leg} E={r.entry:g} RR=1:{r.rr:.1f} -> {detail}{blk}"
                )

        text = "\n".join(lines)
        return text[:4000] + "\n…" if len(text) > 4096 else text
