"""Find every number in a copilot statement (COPILOT.md §7), in English, Hindi (Devanagari) or Hinglish.

Each number carries its value, unit and PRECISION (the place value of the last digit written), so the
validator can accept "1.2 lakh" for ₹1,18,540 but not "1 lakh" (fewer than two significant figures).
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
NUM = r"(?<![\d.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
SCALES = {"k": 3, "thousand": 3, "hazaar": 3, "hazar": 3, "हज़ार": 3, "हजार": 3, "lakh": 5, "lakhs": 5, "lac": 5,
          "lacs": 5, "लाख": 5, "crore": 7, "crores": 7, "cr": 7, "करोड़": 7, "करोड": 7}
SCALE_RE = r"(k|thousand|hazaar|hazar|हज़ार|हजार|lakhs?|lacs?|लाख|crores?|cr|करोड़|करोड)(?![a-z])"
CURRENCY = r"(?:₹|rs\.?|inr|रु\.?)"
RUPEE_WORDS = r"(?:rupees?|rupaye|rupay|rupiya|रुपये|रुपए|रुपया)"
PCT_WORDS = r"(?:%|percent|per\s+cent|pratishat|प्रतिशत|फ़ीसदी|फीसदी|fisadi)"
MONTH_WORDS = r"(?:months?|mahine|mahino|mahina|महीने|महीनों|महीना|mo)(?![a-z])"
DAY_WORDS = r"(?:days?|din|दिन)(?![a-z])"
YEAR_WORDS = r"(?:years?|yrs?|saal|साल|वर्ष)(?![a-z])"
MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
          "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
          "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
          "जनवरी": 1, "फ़रवरी": 2, "फरवरी": 2, "मार्च": 3, "अप्रैल": 4, "मई": 5, "जून": 6, "जुलाई": 7,
          "अगस्त": 8, "सितंबर": 9, "सितम्बर": 9, "अक्टूबर": 10, "नवंबर": 11, "नवम्बर": 11, "दिसंबर": 12,
          "दिसम्बर": 12}
HI_MONTHS = {1: "जनवरी", 2: "फ़रवरी", 3: "मार्च", 4: "अप्रैल", 5: "मई", 6: "जून", 7: "जुलाई", 8: "अगस्त",
             9: "सितंबर", 10: "अक्टूबर", 11: "नवंबर", 12: "दिसंबर"}
MONTH_RE = "(" + "|".join(sorted(map(re.escape, MONTHS), key=len, reverse=True)) + ")"


@dataclass(frozen=True)
class Num:
    text: str
    start: int
    end: int
    unit: str  # inr | pct | months | days | years | date | dom | score | bare
    value: object  # Decimal, or (day | None, month, year | None) for dates
    precision: Decimal  # place value of the last written digit, in the unit
    sig_figs: int


def normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text).translate(DEVANAGARI_DIGITS).replace("​", "").replace("−", "-")


def _dec(s: str) -> tuple[Decimal, Decimal, int]:
    """'1,20,000.50' -> (value, precision, significant figures)."""
    raw = s.replace(",", "")
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    digits = raw.replace(".", "").lstrip("0") or "0"
    return Decimal(raw), Decimal(1).scaleb(-decimals), len(digits)


def extract(text: str) -> list[Num]:
    t = normalise(text)
    low = t.lower()
    found: list[Num] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    def add(m: re.Match, n: Num) -> None:
        if free(m.start(), m.end()):
            found.append(n)
            taken.append((m.start(), m.end()))

    def add_date(m: re.Match, day: int | None, month: int, year: int | None) -> None:
        if day is not None and not 1 <= day <= 31:
            return
        if re.search(r"(?:₹|rs\.?|inr|रु\.?)\s*$", low[:m.start()]):
            return  # "₹50 may be ..." is money, not 50 May
        add(m, Num(m.group(0), m.start(), m.end(), "date", (day, month, year), Decimal(1), 2))

    # dates: "10 Oct 2026", "October 10, 2026", "2026-10-10", "10/10/2026", "Dec 2026"
    for m in re.finditer(rf"(?<!\d)(\d{{1,2}})\s+{MONTH_RE}(?:\s*,?\s*(\d{{4}}))?(?![a-z])", low):
        add_date(m, int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)) if m.group(3) else None)
    for m in re.finditer(rf"(?<![a-z]){MONTH_RE}\s+(\d{{1,2}})(?:\s*,\s*(\d{{4}}))?(?!\d)", low):
        add_date(m, int(m.group(2)), MONTHS[m.group(1)], int(m.group(3)) if m.group(3) else None)
    for m in re.finditer(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", low):
        add_date(m, int(m.group(3)), int(m.group(2)), int(m.group(1)))
    for m in re.finditer(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)", low):
        add_date(m, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in re.finditer(rf"(?<![a-z]){MONTH_RE}\s+(\d{{4}})(?!\d)", low):
        add_date(m, None, MONTHS[m.group(1)], int(m.group(2)))
    # day of month: "the 5th", "5 तारीख"
    for m in re.finditer(r"(?<![\d.])(\d{1,2})\s*(?:st|nd|rd|th|वीं|वी|तारीख|tareekh|tarikh|taarikh)(?![a-z])", low):
        if 1 <= int(m.group(1)) <= 31:
            add(m, Num(m.group(0), m.start(), m.end(), "dom", Decimal(m.group(1)), Decimal(1), 2))
    # money
    money_patterns = [
        rf"{CURRENCY}\s*{NUM}(?:\s*{SCALE_RE})?",
        rf"{NUM}\s*{SCALE_RE}",
        rf"{NUM}\s*{RUPEE_WORDS}",
    ]
    for pat in money_patterns:
        for m in re.finditer(pat, low):
            groups = [g for g in m.groups() if g is not None]
            value, precision, sig = _dec(groups[0])
            scale = SCALES.get(groups[1], 0) if len(groups) > 1 and groups[1] in SCALES else 0
            add(m, Num(m.group(0), m.start(), m.end(), "inr", value.scaleb(scale), precision.scaleb(scale), sig))
    for unit, words in (("pct", PCT_WORDS), ("months", MONTH_WORDS), ("days", DAY_WORDS), ("years", YEAR_WORDS)):
        for m in re.finditer(rf"{NUM}[\s-]*{words}", low):  # "12 months", "12-month"
            value, precision, sig = _dec(m.group(1))
            add(m, Num(m.group(0), m.start(), m.end(), unit, value, precision, sig))
    for m in re.finditer(rf"{NUM}\s*/\s*100(?!\d)", low):
        value, precision, sig = _dec(m.group(1))
        add(m, Num(m.group(0), m.start(), m.end(), "score", value, precision, sig))
    for m in re.finditer(NUM, low):
        if _latin_letter(low[m.start() - 1:m.start()]) or _latin_letter(low[m.end():m.end() + 1]):
            continue  # part of a name or code ("Zee5", "1mg", "24x7"), not a quantity
        value, precision, sig = _dec(m.group(1))
        add(m, Num(m.group(0), m.start(), m.end(), "bare", value, precision, sig))
    return sorted(found, key=lambda n: n.start)


def _latin_letter(ch: str) -> bool:
    return len(ch) == 1 and ch.isascii() and ch.isalpha()


# Spelled-out numbers next to a unit or scale ("do lakh", "two thousand", "एक हज़ार"): numbers must be digits.
_WORD_NUMS = (r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|"
              r"hundred|thousand|ek|do|teen|char|chaar|paanch|panch|chhe|saat|aath|nau|das|bees|sau|pachaas|"
              r"एक|दो|तीन|चार|पाँच|पांच|छह|सात|आठ|नौ|दस|बीस|पचास|सौ")
_UNIT_AFTER = r"(?:lakh|lakhs|lac|crore|thousand|hazaar|hazar|rupees?|rupaye|months?|mahine|percent|pratishat|" \
              r"days?|din|साल|लाख|करोड़|हज़ार|हजार|रुपये|महीने|प्रतिशत|दिन)"


def number_words(text: str) -> list[str]:
    low = normalise(text).lower()
    phrases = list(re.finditer(rf"(?<![\w\d])(?:{_WORD_NUMS})\s+{_UNIT_AFTER}(?![a-z])", low))
    hits = [m.group(0) for m in phrases]
    for m in re.finditer(r"(?:lakhs?|lacs?|crores?|लाख|करोड़|करोड|हज़ार|हजार|hazaar|hazar)(?![a-z])", low):
        inside = any(p.start() <= m.start() < p.end() for p in phrases)
        if not inside and not re.search(r"\d\s*$", low[:m.start()]):  # a scale word with no digit before it
            hits.append(m.group(0))
    return list(dict.fromkeys(hits))


def date_display(d: dt.date, language: str) -> str:
    """How registry dates are shown: '10 Oct 2026', or '10 अक्टूबर 2026' in Hindi."""
    return f"{d.day} {HI_MONTHS[d.month]} {d.year}" if language == "hi" else f"{d.day} {d:%b %Y}"
