"""Normalisation helpers.

Everything the classifier sees passes through `fold()` first, so that
"Trastuzumab-Deruxtecan", "TRASTUZUMAB DERUXTECAN" and "trastuzumab
deruxtecan" are the same string, and Arabic text is stripped of diacritics
and letter-form variants that would otherwise defeat substring matching.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

NA = "Not Available"
REVIEW = "Needs Manual Review"

# Arabic-Indic and Eastern Arabic-Indic digits -> ASCII
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})

_ARABIC_DIACRITICS = re.compile(r"[\u064B-\u0652\u0640]")


def arabic_fold(text: str) -> str:
    """Unify alef/ya/ta-marbuta forms and drop diacritics and tatweel."""
    text = _ARABIC_DIACRITICS.sub("", text)
    for src, dst in (("أإآٱ", "ا"), ("ىٰ", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي")):
        for ch in src:
            text = text.replace(ch, dst)
    return text


def fold(text: str | None) -> str:
    """Canonical form used for all keyword matching."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = text.translate(_DIGIT_MAP)
    text = arabic_fold(text)
    text = text.lower()
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", text)   # bidi marks
    text = re.sub(r"[^\w\u0600-\u06FF%./+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str | None) -> list[str]:
    return [t for t in re.split(r"[^\w\u0600-\u06FF]+", fold(text)) if t]


def clean(text: str | None) -> str:
    """Tidy a value for storage/display without destroying its original form."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = re.sub(r"[\u200b-\u200f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- dates

_DATE_PATTERNS = [
    ("%Y-%m-%d", r"\d{4}-\d{1,2}-\d{1,2}"),
    ("%d/%m/%Y", r"\d{1,2}/\d{1,2}/\d{4}"),
    ("%d-%m-%Y", r"\d{1,2}-\d{1,2}-\d{4}"),
    ("%d.%m.%Y", r"\d{1,2}\.\d{1,2}\.\d{4}"),
    ("%m/%d/%Y", r"\d{1,2}/\d{1,2}/\d{4}"),
    ("%d %b %Y", r"\d{1,2} [A-Za-z]{3,9} \d{4}"),
    ("%d %B %Y", r"\d{1,2} [A-Za-z]{3,9} \d{4}"),
    ("%b %d, %Y", r"[A-Za-z]{3,9} \d{1,2}, \d{4}"),
]


def parse_date(value, dayfirst: bool = True) -> str | None:
    """Return ISO yyyy-mm-dd, or None. Kuwait forms are day-first by default."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).translate(_DIGIT_MAP).strip()
    text = re.sub(r"\s+", " ", text)
    # strip a trailing time component
    text = re.sub(r"\s+\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?$", "", text)

    patterns = _DATE_PATTERNS
    if not dayfirst:
        patterns = sorted(patterns, key=lambda p: p[0] != "%m/%d/%Y")

    for fmt, rx in patterns:
        m = re.search(rx, text)
        if not m:
            continue
        try:
            return datetime.strptime(m.group(0), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_time(value) -> str | None:
    if not value:
        return None
    text = str(value).translate(_DIGIT_MAP)
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def days_until(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    try:
        return (date.fromisoformat(iso_date) - date.today()).days
    except ValueError:
        return None


# ---------------------------------------------------------------- numbers

_CURRENCIES = {
    "kwd": "KWD", "kd": "KWD", "د.ك": "KWD", "دينار": "KWD",
    "usd": "USD", "$": "USD", "eur": "EUR", "€": "EUR",
    "sar": "SAR", "aed": "AED", "gbp": "GBP", "£": "GBP",
}


def parse_amount(value) -> tuple[float | None, str | None]:
    """Return (amount, currency) from messy strings like '1,250.500 KD'."""
    if value in (None, ""):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None

    text = str(value).translate(_DIGIT_MAP)
    low = text.lower()
    currency = None
    for token, code in _CURRENCIES.items():
        if token in low:
            currency = code
            break

    m = re.search(r"-?\d[\d,\s]*\.?\d*", text.replace("\u066b", "."))
    if not m:
        return None, currency
    num = m.group(0).replace(",", "").replace(" ", "")
    try:
        return float(num), currency
    except ValueError:
        return None, currency


def parse_quantity(value) -> tuple[float | None, str | None]:
    """'50,000 vials' -> (50000.0, 'vials')."""
    if value in (None, ""):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    text = str(value).translate(_DIGIT_MAP).strip()
    m = re.match(r"\s*([\d,\.\s]+)\s*(.*)$", text)
    if not m:
        return None, None
    try:
        qty = float(m.group(1).replace(",", "").replace(" ", ""))
    except ValueError:
        return None, clean(text) or None
    unit = clean(m.group(2)) or None
    return qty, unit


# ------------------------------------------------- pharma-specific parsing

DOSAGE_FORMS = [
    "tablet", "film-coated tablet", "capsule", "soft capsule", "vial",
    "ampoule", "prefilled syringe", "pre-filled syringe", "pen", "injection",
    "solution for injection", "solution for infusion", "powder for solution",
    "lyophilised powder", "lyophilized powder", "oral solution", "syrup",
    "suspension", "sachet", "suppository", "cream", "ointment", "gel",
    "eye drops", "ear drops", "nasal spray", "inhaler", "nebuliser solution",
    "patch", "infusion bag", "concentrate",
]

_STRENGTH_RX = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*"
    r"(mcg|µg|ug|mg|g|kg|ml|l|iu|u|%|mg/ml|mg/m2|mcg/ml|g/l|mmol|meq)"
    r"(?:\s*/\s*(\d+(?:[.,]\d+)?)?\s*(ml|l|g|mg|dose|vial|tab|capsule))?\b",
    re.IGNORECASE,
)


def extract_strength(text: str | None) -> str | None:
    """Pull '100 mg/5 mL' style strength out of a product description."""
    if not text:
        return None
    m = _STRENGTH_RX.search(str(text).translate(_DIGIT_MAP))
    return clean(m.group(0)) if m else None


def extract_dosage_form(text: str | None) -> str | None:
    folded = fold(text)
    if not folded:
        return None
    best = None
    for form in DOSAGE_FORMS:
        if form in folded and (best is None or len(form) > len(best)):
            best = form
    return best


_PACK_RX = re.compile(
    r"\b(?:pack\s*(?:of|size)?\s*)?(\d+)\s*(?:x|\*|×)\s*(\d+(?:[.,]\d+)?\s*"
    r"(?:mg|ml|g|mcg|iu)?)\b|\bbox\s*of\s*(\d+)\b|\b(\d+)\s*(?:tabs?|tablets?|"
    r"caps?|capsules?|vials?|amps?|ampoules?)\s*(?:/|per)?\s*(?:pack|box)\b",
    re.IGNORECASE,
)


def extract_pack_size(text: str | None) -> str | None:
    if not text:
        return None
    m = _PACK_RX.search(str(text).translate(_DIGIT_MAP))
    return clean(m.group(0)) if m else None


def coalesce(*values, default: str = NA):
    for v in values:
        if v not in (None, "", []):
            return v
    return default
