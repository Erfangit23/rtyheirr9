"""
Test: signal parser robustness.

Bug being guarded (seen in a live /backtest run):
    ❌ Backtest failed for @BrianTradingForex: could not convert string to float: '.'
The numeric patterns used [\\d.]+ which matches a lone ".", so a message with a
missing/edited price ("Take Profit : .") raised ValueError and killed the whole
backtest (and in live mode would drop the Telegram event).

Verifies:
  - every parser survives messages whose price fields are just "." (no exception)
  - such messages return None instead of a bogus Signal
  - all 8 documented formats still parse correctly (regression)
  - parse_signal never raises, even on truncated/garbage input

Run:  python test_parser_robustness.py
"""

import sys
import os

sys.modules.setdefault("MetaTrader5", type(sys)("MetaTrader5"))

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

from signal_parser import parse_signal, PARSERS          # noqa: E402


# --- messages that used to crash the parser -------------------------------
DOT_MESSAGES = [
    "📊XAUUSD SELL NOW ( . ) ✅\n📊TARGET 1 ( . )✅\n🚫 STOP LOSS ( . )",
    "XAUUSD SELL NOW .:::. \n✔️ Tp1 🔽 .\n❌ SL .",
    "XAUUSD Sell .\n\nTP .\nTP .\n\nSL .",
    "XAU/USD Buy . - .\n📌 Stoploss : .\n- Take Profit : .",
    "XAUUSD ( اسکلپ )\nBuy now : . - .\nTp1 : .\nSl : .",
    "⚜️ XAUUSD - BUY NOW\n🛒 Entry : .\n🎯 Targets :\n.\n🔺 Stoploss : .",
    "XAUUSD | SELL 📈\nEntry : .\n✔️TP1: .\n❌SL : .",
    "GOLD Buy Limit . - .\nTP .\nSL .",
    "XAUUSD . . . .",
    "XAU/USD Buy 4063 - .\nStoploss : .\nTake Profit : .",
    "XAUUSD Sell 4064\nTP 4059\nSL .",
    # truncated mid-message
    "XAUUSD SELL NOW ( 4167 )\n🚫 STOP LOSS (",
    "XAU/USD Buy 4063 - 4060\nTake Profit :",
]

# --- one known-good message per format ------------------------------------
GOOD = {
    "format1": "📊XAUUSD SELL NOW ( 4167 ) ✅\n📊TARGET 1 ( 4163 )✅\n"
               "📊TARGET 2 ( 4159 )✅\n🚫 STOP LOSS ( 4177 )",
    "format2": "XAUUSD SELL NOW 4171:::4180\n✔️ Tp1 🔽 4166\n✔️ Tp2 🔽 4161\n"
               "❌ SL 4186 100% Sure Call",
    "format3": "XAUUSD Sell 4064\n\nTP 4059\nTP 4054\nTP 4049\n\n\nSL 4074",
    "format4": "Gold Trader Alliance ❤\nTrade Setup #04 - July 21\n\n"
               "🧑‍💻 XAU/USD Buy 4063 - 4060\n📌 Stoploss : 4057\n"
               "- Take Profit : 4068 ( 70pips )\n- Take Profit : 4085 ( 380pips )",
    "format5": "XAUUSD ( اسکلپ )\n\nMarket price : 4090\n\nBuy now : 4090 - 4086\n\n"
               "Tp1 : 4096\nTp2 : 4106\nTp3 : 4122\nTp4 : open\n\nSl : 4080 ( 80 pip )",
    "format6": "⚜️ XAUUSD - BUY NOW\n\n🛒 Entry : 4081\n\n🎯 Targets :\n4086\n4091\nOPEN\n\n"
               "🔺 Stoploss : 4076",
    "format7": "XAUUSD | SELL 📈\n\nEntry : 4043\n\n✔️TP1: 4039\n✔️TP2: 4035\n"
               "✔️TP3: 4031\n🚀TP4: 4027\n\n❌SL : 4052",
    "format8": "GOLD Buy Limit 4252-4253\n\nTP 4259\nTP 4300\n\nSL 4246",
}

EXPECTED = {
    "format1": ("SELL", 4167.0, 4177.0, [4163.0, 4159.0]),
    "format2": ("SELL", 4171.0, 4180.0, [4166.0, 4161.0]),
    "format3": ("SELL", 4064.0, 4074.0, [4059.0, 4054.0, 4049.0]),
    "format4": ("BUY", 4063.0, 4057.0, [4068.0, 4085.0]),
    "format5": ("BUY", 4090.0, 4080.0, [4096.0, 4106.0, 4122.0]),
    "format6": ("BUY", 4081.0, 4076.0, [4086.0, 4091.0]),
    "format7": ("SELL", 4043.0, 4052.0, [4039.0, 4035.0, 4031.0, 4027.0]),
    "format8": ("BUY", 4252.0, 4246.0, [4259.0, 4300.0]),
}


def _check(failures, label, cond):
    print(("  PASS: " if cond else "  FAIL: ") + label)
    if not cond:
        failures.append(label)


def main():
    failures = []

    print("-- dot-only / broken messages must not raise --")
    for text in DOT_MESSAGES:
        label = text.replace("\n", " | ")[:58]
        try:
            sig = parse_signal(text, "@BrianTradingForex", "format4")
            # also exercise every other parser directly
            for name, parser in PARSERS.items():
                parser(text, "@test")
            ok = sig is None
            _check(failures, f"no crash + None: {label}", ok)
        except Exception as e:
            _check(failures, f"no crash + None: {label} ({type(e).__name__}: {e})", False)

    print("\n-- no parser may ever raise --")
    for name, parser in PARSERS.items():
        try:
            parser("", "@test")
            parser("XAUUSD", "@test")
            parser("XAUUSD " + "." * 50, "@test")
            parser("XAUUSD BUY SELL TP SL ::: . . .", "@test")
            _check(failures, f"{name}: survives garbage", True)
        except Exception as e:
            _check(failures, f"{name}: survives garbage ({type(e).__name__}: {e})", False)

    print("\n-- all 8 formats still parse correctly (regression) --")
    for fmt, text in GOOD.items():
        sig = parse_signal(text, "@test", fmt)
        exp = EXPECTED[fmt]
        if sig is None:
            _check(failures, f"{fmt}: parsed", False)
            continue
        got = (sig.direction, sig.entry, sig.stop_loss, list(sig.take_profits))
        _check(failures, f"{fmt}: {got[0]} entry={got[1]} sl={got[2]} tps={got[3]}", got == exp)

    print("\n-- auto mode still finds the right parser --")
    for fmt, text in GOOD.items():
        sig = parse_signal(text, "@test", "auto")
        _check(failures, f"auto: {fmt} message parsed", sig is not None)

    print("\n-- a dot inside a TP list does not poison the others --")
    text = ("XAU/USD Buy 4063 - 4060\n📌 Stoploss : 4057\n"
            "- Take Profit : 4068 ( 70pips )\n- Take Profit : . ( 380pips )")
    sig = parse_signal(text, "@test", "format4")
    _check(failures, "entry/SL parsed, dot TP skipped",
           sig is not None and sig.entry == 4063.0 and sig.stop_loss == 4057.0
           and sig.take_profits == [4068.0])

    print("\n-- implausible levels (pip counts etc.) are dropped or rejected --")
    # "TP 100" is a pip count, not a price: drop it, keep the real TP
    sig = parse_signal("XAUUSD Sell 4064\n\nTP 4059\nTP 100\n\nSL 4074", "@test", "format3")
    _check(failures, f"garbage TP dropped, real TP kept (got {sig.take_profits if sig else None})",
           sig is not None and sig.take_profits == [4059.0])

    # an implausible SL makes the whole signal unusable
    sig = parse_signal("XAUUSD Sell 4064\n\nTP 4059\n\nSL 100", "@test", "format3")
    _check(failures, "implausible SL -> signal discarded", sig is None)

    # a normal signal is untouched by the plausibility filter
    sig = parse_signal(GOOD["format3"], "@test", "format3")
    _check(failures, "normal signal unaffected",
           sig is not None and sig.take_profits == [4059.0, 4054.0, 4049.0])

    print("=" * 40)
    if failures:
        print(f"RESULT: {len(failures)} check(s) FAILED")
        sys.exit(1)
    print("RESULT: ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    print("=== signal parser robustness test ===")
    main()
