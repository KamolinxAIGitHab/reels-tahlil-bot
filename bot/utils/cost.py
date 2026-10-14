"""OpenAI xarajatini hisoblash va yozish.

Narxlar taxminiy (USD, 1 mln token uchun) va muhit o'zgaruvchilari orqali
almashtiriladi: COST_PRICES_JSON, STT_PRICE_PER_MIN, COST_ADJUST.
Rasmiy narxlar: https://platform.openai.com/docs/pricing
Yozishda xato bo'lsa bot ishini to'xtatmaydi: faqat logga yoziladi.
"""
import json
import logging
import os

from bot.utils import stats

# model: (kiritish, keshlangan kiritish, chiqarish) — 1 mln token uchun USD
_DEFAULT_PRICES = {
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
}

def _env_float(name: str, default: float) -> float:
    """Muhit o'zgaruvchisidan son o'qiydi. Kasr belgisi nuqta ham, vergul ham
    bo'lishi mumkin ("0.03" yoki "0,03"). Noto'g'ri qiymatda standart ishlatiladi."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip().replace(",", "."))
    except ValueError:
        logging.warning(f"{name}={raw!r} noto'g'ri qiymat, standart {default} ishlatildi")
        return default


env_float = _env_float  # boshqa modullar uchun ochiq nom


# gpt-4o-mini-transcribe: daqiqasiga taxminiy narx (USD)
STT_PRICE_PER_MIN = _env_float("STT_PRICE_PER_MIN", 0.003)
# OpenAI Usage sahifasi bilan taqqoslab tuzatish koeffitsienti (1.0 = o'zgartirmaslik)
COST_ADJUST = _env_float("COST_ADJUST", 1.0)


# Kunlik va oylik limit (USD). 0 — o'chirilgan. Limitga yetganda faqat adminga
# ogohlantirish yuboriladi, foydalanuvchilar bloklanmaydi.
LIMIT_DAY = _env_float("COST_LIMIT_DAY", 0.5)
LIMIT_MONTH = _env_float("COST_LIMIT_MONTH", 8.0)
_THRESHOLDS = (80, 100)

_unknown_warned: set[str] = set()


def _load_prices() -> dict:
    prices = dict(_DEFAULT_PRICES)
    raw = os.getenv("COST_PRICES_JSON", "").strip()
    if raw:
        try:
            for name, vals in json.loads(raw).items():
                prices[name] = tuple(float(v) for v in vals)
        except Exception as e:
            logging.warning(f"COST_PRICES_JSON o'qilmadi, standart narxlar ishlatiladi: {e}")
    return prices


PRICES = _load_prices()


def _price_for(model: str) -> tuple:
    # Eng uzun nom oldin tekshiriladi: "gpt-4o-mini-2024..." "gpt-4o" dan oldin topilsin
    for name in sorted(PRICES, key=len, reverse=True):
        if model.startswith(name):
            return PRICES[name]
    if model not in _unknown_warned:
        _unknown_warned.add(model)
        logging.warning(f"COST: '{model}' narxi noma'lum, gpt-4o narxi ishlatildi")
    return PRICES["gpt-4o"]


def chat_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    p_in, p_cached, p_out = _price_for(model)
    fresh = max(input_tokens - cached_tokens, 0)
    usd = (fresh * p_in + cached_tokens * p_cached + output_tokens * p_out) / 1_000_000
    return usd * COST_ADJUST


def _usd(v: float) -> str:
    return f"${v:.2f}" if v >= 0.1 else f"${v:.3f}"


def check_limits() -> None:
    """Kunlik/oylik limitning 80% va 100% chegaralarini tekshiradi va yangi
    ogohlantirishni navbatga yozadi (yuborishni main.py dagi sikl qiladi).
    Bir chegara kunga/oyga faqat bir marta yoziladi."""
    try:
        from datetime import datetime, timedelta, timezone
        today_usd, month_usd = stats.cost_totals()
        now_uz = datetime.now(timezone.utc) + timedelta(hours=5)
        periods = (
            ("day", now_uz.strftime("%Y-%m-%d"), "Бугунги", today_usd, LIMIT_DAY),
            ("month", now_uz.strftime("%Y-%m"), "Ойлик", month_usd, LIMIT_MONTH),
        )
        for kind, period, title, spent, limit in periods:
            if limit <= 0:
                continue
            crossed = [t for t in _THRESHOLDS if spent >= limit * t / 100]
            for t in crossed:
                key = f"{kind}:{period}:{t}"
                # Битта чақирувда бир нечта чегара ошилса, фақат энг юқориси юборилади
                send_it = t == crossed[-1]
                if t == 100:
                    text = (
                        f"🚨 {title} харажат лимитидан ОШДИ: {_usd(spent)} / {_usd(limit)}.\n"
                        "Бот ишлашда давом этмоқда, фойдаланувчилар блокланмайди. "
                        "OpenAI қаттиқ лимити алоҳида ишлайди."
                    )
                else:
                    text = f"⚠️ {title} харажат лимитининг {t}% га етди: {_usd(spent)} / {_usd(limit)}."
                # sent: 0 — юборилиши керак, 2 — жим ёзилган (юқорироқ чегара билан бирга ошилган)
                added = stats.add_alert(key, text, sent=0 if send_it else 2)
                if added and send_it:
                    logging.info(f"COST: огоҳлантириш навбатга ёзилди ({key})")
    except Exception as e:
        logging.warning(f"COST limit текшируви хато берди: {e}")


def record_chat(response, label: str) -> None:
    """chat.completions javobidagi usage'dan xarajatni yozadi."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        model = getattr(response, "model", "") or "unknown"
        inp = int(getattr(usage, "prompt_tokens", 0) or 0)
        out = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        stats.log_cost(label, model, inp, cached, out, 0.0, chat_cost(model, inp, cached, out))
        check_limits()
    except Exception as e:
        logging.warning(f"COST yozilmadi ({label}): {e}")


def record_audio(seconds: float | None, model: str, label: str = "stt") -> None:
    """Ovoz transkripsiyasi xarajatini davomiylikdan hisoblab yozadi."""
    try:
        sec = float(seconds or 0.0)
        stats.log_cost(label, model, 0, 0, 0, sec, sec / 60.0 * STT_PRICE_PER_MIN * COST_ADJUST)
        check_limits()
    except Exception as e:
        logging.warning(f"COST yozilmadi ({label}): {e}")
