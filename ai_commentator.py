"""
AI Trade Commentary — generates human-like Persian trade descriptions using DeepSeek V4 Flash.

Sends personalized, emotional trade commentary to a specific Telegram user via DM.
Each trade event (placed, filled, TP hit, SL hit, cancelled) gets a unique,
human-sounding message in Persian/Farsi.
"""

import logging
from typing import Optional

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_API_KEY = "nvapi-…uPdk"
NVIDIA_MODEL = "deepseek-ai/deepseek-v4-flash"

COMMENTARY_SYSTEM_PROMPT = """تو یک دستیار معاملات طلا هستی که به فارسی و با لحن طبیعی و انسانی صحبت می‌کنی.

برای هر رویداد معامله، یک پیام کوتاه (۱-۳ جمله) به فارسی بنویس که:
- طبیعی و انسانی باشه، نه رباتی
- احساسات رو بیان کنه (هیجان، نگرانی، رضایت، ناراحتی)
- جزئیات کلیدی معامله رو شامل بشه (جهت، قیمت ورود، حد ضرر، حد سود)
- کوتاه و مفید باشه

قوانین:
- فقط فارسی بنویس
- از ایموجی استفاده کن اما زیاده‌روی نکن (۱-۲ تا)
- لحن صمیمی و دوستانه
- نام canal رو ذکر کن
- اگر سود گرفت: خوشحال باش، اگر ضرر داد: ناراحت ولی امیدوار
- اگر سفارش لغو شد: منطقی توضیح بده
- فقط پیام رو بنویس، هیچ چیز اضافه‌ای نذار"""


class AICommentator:
    """Generates human-like Persian trade commentary using AI."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("xau_trader")
        self.client = None
        if OpenAI:
            self.client = OpenAI(
                base_url=NVIDIA_BASE_URL,
                api_key=NVIDIA_API_KEY,
            )
            self.logger.info("AI Commentator initialized")
        else:
            self.logger.warning("OpenAI SDK not installed. AI commentary unavailable.")

    def is_available(self) -> bool:
        return self.client is not None

    def generate_commentary(self, event_type: str, trade_data: dict) -> Optional[str]:
        """
        Generate a human-like Persian commentary for a trade event.

        event_type: "placed", "filled", "tp_hit", "sl_hit", "cancelled", "rejected_sl"
        trade_data: dict with direction, symbol, entry, sl, tp, channel, profit, etc.

        Returns Persian text or None on failure.
        """
        if not self.client:
            return None

        # Build the user prompt based on event type
        event_descriptions = {
            "placed": "سفارش جدید ثبت شد",
            "filled": "سفارش فعال شد (پوزیشن باز شد)",
            "tp_hit": "حد سود برخورد کرد",
            "sl_hit": "حد ضرر برخورد کرد",
            "cancelled": "سفارش لغو شد",
            "rejected_sl": "سفارش رد شد چون حد ضرر بیش از حد زیاد بود",
        }

        event_desc = event_descriptions.get(event_type, event_type)

        # Build trade details
        details = f"رویداد: {event_desc}\n"
        details += f"کانال: {trade_data.get('channel', 'نامشخص')}\n"
        details += f"نوع: {trade_data.get('direction', '')} {trade_data.get('symbol', 'XAUUSD')}\n"
        details += f"قیمت ورود: {trade_data.get('entry', 'نامشخص')}\n"
        details += f"حد ضرر: {trade_data.get('sl', 'نامشخص')}\n"
        details += f"حد سود: {trade_data.get('tp', 'نامشخص')}\n"

        if trade_data.get('profit') is not None:
            details += f"سود/ضرر: {trade_data['profit']:.2f} دلار\n"

        if trade_data.get('lot_size'):
            details += f"حجم: {trade_data['lot_size']}\n"

        if trade_data.get('reason'):
            details += f"دلیل: {trade_data['reason']}\n"

        try:
            response = self.client.chat.completions.create(
                model=NVIDIA_MODEL,
                messages=[
                    {"role": "system", "content": COMMENTARY_SYSTEM_PROMPT},
                    {"role": "user", "content": details},
                ],
                temperature=0.8,  # Higher temperature for more varied/human responses
                top_p=0.95,
                max_tokens=256,
                extra_body={
                    "chat_template_kwargs": {
                        "thinking": False,
                    }
                },
                stream=False,
                timeout=6,
            )

            content = response.choices[0].message.content.strip()
            return content

        except Exception as e:
            self.logger.warning(f"AI commentary error: {e}")
            return None
