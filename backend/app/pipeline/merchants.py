"""Merchant normalisation from UPI / NACH / POS / NEFT / IMPS / card narrations (SPEC §5.1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.config import CONFIG_DIR, load_yaml

_UPI_SLASH = re.compile(r"^UPI/(DR|CR)/(\d+)/([^/]*)/([^/]*)/([^/]*)/?(.*)$", re.IGNORECASE)
_UPI_DASH = re.compile(r"^UPI-([^-]*)-([^-]*@[^-]*)-([A-Z]{4}0[A-Z0-9]{6})-(\d+)-?(.*)$", re.IGNORECASE)
_NACH = re.compile(r"^(?:NACH|ACH|ECS)[ /\-]*(?:DR|D|DEBIT)?[ /\-]+(.+?)(?:[-/](?:[A-Z]{4}\d{10,}|\d{6,})\S*)?$",
                   re.IGNORECASE)
_NEFT = re.compile(r"^(NEFT|RTGS)[ \-]*(CR|DR)?-([A-Z]{4}0[A-Z0-9]{6})-([^-]+)-?([^-]*)-?(.*)$", re.IGNORECASE)
_IMPS_SLASH = re.compile(r"^IMPS/P2A/(\d+)/([^/]*)/([^/]*)/?(.*)$", re.IGNORECASE)
_IMPS_DASH = re.compile(r"^IMPS-(\d+)-([^-]*)-?(.*)$", re.IGNORECASE)
_POS = re.compile(r"^POS\s+[\dX*]{8,}\s+(.+?)(?:\s+[A-Z]{3,})?$", re.IGNORECASE)
_BIL = re.compile(r"^BIL/(?:ONL|BPAY|INFT)/\d*/([^/]*)/?(.*)$", re.IGNORECASE)
_ATM = re.compile(r"^(ATW|NWD|ATM)[\-/ ]", re.IGNORECASE)
_LOAN_DISB = re.compile(r"^LOAN DISB[/\- ]+([^/\-]+)", re.IGNORECASE)
_LAST4 = re.compile(r"(?:X{2,}|\*{2,}|A/?C\s*)(\d{4})\b", re.IGNORECASE)
_PHONE_LOCAL = re.compile(r"^\+?\d{10,12}$")


@dataclass
class Parsed:
    channel: str  # upi | nach | neft | imps | pos | card | billpay | atm | cash | other
    payee: str  # raw payee string (may contain a personal name: PII)
    vpa: str | None = None
    remark: str = ""
    reference: str | None = None
    last4_ref: str | None = None
    signatures: set[str] = field(default_factory=set)


@dataclass
class Resolved:
    merchant_key: str
    merchant_name: str
    counterparty_type: str  # merchant | person | self | bank
    payee_clean: str
    person_keys: list[str] = field(default_factory=list)  # match keys, for counterparty pseudonyms


@lru_cache
def _merchant_cfg():
    cfg = load_yaml("merchants")
    aliases = sorted(((a.upper(), k) for k, m in cfg["merchants"].items() for a in m["aliases"]),
                     key=lambda x: (-len(x[0]), x[0]))
    return cfg, aliases, set(cfg["business_words"]), set(cfg["noise_words"])


@lru_cache
def first_names() -> frozenset[str]:
    words = []
    for line in (CONFIG_DIR / "names_in.txt").read_text().splitlines():
        if not line.startswith("#"):
            words += line.split()
    return frozenset(words)


@lru_cache
def _signatures() -> dict[str, list[re.Pattern]]:
    sig = load_yaml("categories")["signatures"]
    return {k: [re.compile(rf"(?<![A-Z]){re.escape(s.upper())}(?![A-Z])") for s in v] for k, v in sig.items()}


def detect_signatures(text: str) -> set[str]:
    up = text.upper()
    return {k for k, pats in _signatures().items() if any(p.search(up) for p in pats)}


def clean(text: str) -> str:
    """Upper-case, punctuation -> space, collapse. Keeps words; used for alias matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", text.upper())).strip()


def strip_noise(text: str) -> str:
    _, _, _, noise = _merchant_cfg()
    return " ".join(t for t in clean(text).split() if t not in noise)


def parse_narration(narration: str, merchant_hint: str | None = None, channel_hint: str | None = None,
                    account_kind: str | None = None) -> Parsed:
    n = narration.strip()
    if merchant_hint is not None:
        hint = merchant_hint.strip()
        # SMS credit/mandate payees are themselves bank narrations ("NEFT Cr-...", "NACH-DR-...").
        if re.match(r"^(NEFT|RTGS|NACH|ACH|ECS|IMPS|UPI|BIL|POS)", hint, re.IGNORECASE):
            p = parse_narration(hint, None, channel_hint, account_kind)
            return p
        vpa = hint if "@" in hint else None
        payee = hint.split("@")[0].replace(".", " ").replace("_", " ") if vpa else hint
        ch = channel_hint or ("upi" if vpa else ("card" if account_kind == "credit_card" else "other"))
        if narration.startswith("MANUAL/"):
            ch = channel_hint or "cash"
        return Parsed(ch, payee, vpa=vpa, signatures=detect_signatures(hint))

    sigs = detect_signatures(n)
    last4 = (m.group(1) if (m := _LAST4.search(n)) else None)
    if m := _UPI_SLASH.match(n):
        return Parsed("upi", m.group(3), vpa=m.group(5) or None, remark=m.group(6), reference=m.group(2),
                      last4_ref=last4, signatures=sigs)
    if m := _UPI_DASH.match(n):
        return Parsed("upi", m.group(1), vpa=m.group(2), remark=m.group(5), reference=m.group(4),
                      last4_ref=last4, signatures=sigs)
    if m := _NEFT.match(n):
        return Parsed("neft", m.group(4), remark=f"{m.group(5)} {m.group(6)}".strip(), last4_ref=last4,
                      signatures=sigs)
    if m := _IMPS_SLASH.match(n):
        return Parsed("imps", m.group(2), remark=f"{m.group(3)} {m.group(4)}".strip(), reference=m.group(1),
                      last4_ref=last4, signatures=sigs)
    if m := _IMPS_DASH.match(n):
        return Parsed("imps", m.group(2), remark=m.group(3), reference=m.group(1), last4_ref=last4,
                      signatures=sigs)
    if m := _NACH.match(n):
        return Parsed("nach", m.group(1), last4_ref=last4, signatures=sigs)
    if m := _BIL.match(n):
        return Parsed("billpay", m.group(1), remark=m.group(2), last4_ref=last4, signatures=sigs)
    if m := _POS.match(n):
        return Parsed("pos", m.group(1), last4_ref=None, signatures=sigs)
    if _ATM.match(n):
        return Parsed("atm", "ATM", signatures=sigs)
    if m := _LOAN_DISB.match(n):
        return Parsed("neft", m.group(1), signatures=sigs)
    ch = "card" if account_kind == "credit_card" else (channel_hint or "other")
    return Parsed(ch, n, last4_ref=last4, signatures=sigs)


def match_alias(*texts: str) -> str | None:
    _, aliases, _, _ = _merchant_cfg()
    padded = [f" {clean(t)} " for t in texts if t]
    for alias, key in aliases:
        a = f" {clean(alias)} "
        if any(a in p for p in padded):
            return key
    return None


def looks_like_person(payee_clean: str, vpa: str | None, channel: str) -> bool:
    _, _, business, _ = _merchant_cfg()
    tokens = payee_clean.split()
    if any(t in business for t in tokens):
        return False
    if vpa:
        local = vpa.split("@")[0]
        if _PHONE_LOCAL.match(local):
            return True
    if not tokens or len(tokens) > 4 or not all(t.isalpha() for t in tokens):
        return False
    if tokens[0] in first_names():
        return True
    return channel in ("neft", "imps") and 2 <= len(tokens) <= 3


def slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", text.lower())).strip("_")[:60] or "unknown"


def resolve(p: Parsed, holder_names: frozenset[str] = frozenset()) -> Resolved:
    """Map a parsed narration to a canonical merchant, or flag it as a person / self."""
    cfg, _, _, _ = _merchant_cfg()
    payee_clean = strip_noise(p.payee)
    vpa_local = p.vpa.split("@")[0].replace(".", " ").replace("_", " ") if p.vpa else ""
    if clean(p.payee) in holder_names:
        return Resolved("self", "Own account", "self", payee_clean)
    key = match_alias(p.payee, vpa_local)
    if key:
        return Resolved(key, cfg["merchants"][key]["name"], "merchant", payee_clean)
    if p.channel == "atm":
        return Resolved("atm", "ATM", "bank", "ATM")
    if looks_like_person(payee_clean or clean(vpa_local), p.vpa, p.channel):
        keys = []
        if p.vpa:
            keys.append(f"vpa:{p.vpa.lower()}")
        name = payee_clean or clean(vpa_local)
        if name and not _PHONE_LOCAL.match(name.replace(" ", "")):
            keys.append(f"name:{name}")
        return Resolved("p2p", "Person", "person", payee_clean, person_keys=keys)
    name = payee_clean or clean(p.payee) or "UNKNOWN"
    return Resolved(f"m:{slug(name)}", name.title(), "merchant", name)
