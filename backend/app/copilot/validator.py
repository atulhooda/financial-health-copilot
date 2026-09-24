"""The validator (COPILOT.md §7): an answer passes only if every number traces to this turn's registry.

Rules, each with a code; any error blocks the answer. The error list goes back to the LLM as the tool result of the
failed `respond`, so the details say what to fix.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.copilot import numbers
from app.copilot.language import LANGUAGES, TOKEN, hinglish_hits, script_fits
from app.copilot.registry import Entry, Registry
from app.core.config import load_yaml
from app.core.pii import find_regex_pii

LABELS = ("FACT", "PREDICTION", "RECOMMENDATION")
MAX_STATEMENTS = 8
MAX_TEXT = 400
ALLOWED_KINDS = {
    "FACT": {"fact", "user_input"},
    "PREDICTION": {"prediction", "fact", "user_input", "assumption"},
    "RECOMMENDATION": {"recommendation", "assumption", "fact", "user_input"},
}
PSEUDONYM = re.compile(r"(?i)\bcontact[ _]\d+\b")  # a counterparty pseudonym is a name, not a quantity
WORD = re.compile(r"[\wऀ-ॿ]+")


@dataclass
class Issue:
    statement: int | None  # 1-based; None for the whole answer
    code: str
    detail: str


@dataclass
class Verdict:
    ok: bool
    errors: list[Issue]

    def tool_result(self) -> dict:
        return {"ok": self.ok, "errors": [asdict(e) for e in self.errors]}


def _schema(candidate: object) -> list[Issue]:
    if not isinstance(candidate, dict):
        return [Issue(None, "SCHEMA", "respond input must be an object")]
    errs = []
    if candidate.get("language") not in LANGUAGES:
        errs.append(Issue(None, "SCHEMA", f"language must be one of {list(LANGUAGES)}"))
    sts = candidate.get("statements")
    if not isinstance(sts, list) or not 1 <= len(sts) <= MAX_STATEMENTS:
        return errs + [Issue(None, "SCHEMA", f"statements must be a list of 1 to {MAX_STATEMENTS} items")]
    for i, st in enumerate(sts, 1):
        if not isinstance(st, dict):
            errs.append(Issue(i, "SCHEMA", "a statement must be an object"))
            continue
        if st.get("label") not in LABELS:
            errs.append(Issue(i, "SCHEMA", f"label must be one of {list(LABELS)}"))
        if not isinstance(st.get("text"), str) or not st["text"].strip():
            errs.append(Issue(i, "SCHEMA", "text must be a non-empty string"))
        elif len(st["text"]) > MAX_TEXT:
            errs.append(Issue(i, "SCHEMA", f"text is {len(st['text'])} characters; the limit is {MAX_TEXT}"))
        if not isinstance(st.get("refs"), list) or not all(isinstance(r, str) for r in st["refs"]):
            errs.append(Issue(i, "SCHEMA", "refs must be a list of registry ids"))
    return errs


def _for_numbers(text: str) -> str:
    """Masked tokens and pseudonyms carry digits that are names, not claims."""
    return PSEUDONYM.sub(" ", TOKEN.sub(" ", text))


def _hint(n: numbers.Num, reg: Registry, language: str) -> str:
    same = [e for e in reg.entries.values() if e.numeric and _unit_family(e.unit) == _unit_family(n.unit)]
    if not same or n.unit in ("date", "dom"):
        return ""
    try:
        best = min(same, key=lambda e: abs(abs(e.value) - n.value) / max(abs(e.value), 1))
    except TypeError:
        return ""
    return f"; the closest registry number is {best.id} '{best.display(language)}' ({best.desc})"


def _unit_family(unit: str) -> str:
    return {"bare": "count", "score": "count", "dom": "date"}.get(unit, unit)


def _confidence_label_stated(text: str, language: str) -> str | None:
    """The label a statement gives next to a confidence word ('Medium confidence', 'भरोसा: मध्यम'), or None."""
    cfg = load_yaml("copilot")["confidence"]
    words = [w.lower() for w in WORD.findall(numbers.normalise(text))]
    nouns = {n.lower() for lang in {language, "en"} for n in cfg["nouns"].get(lang, [])}
    label_of = {w.lower(): label for label, per in cfg["labels"].items()
                for lang in {language, "en"} for w in per.get(lang, [])}
    for i, w in enumerate(words):
        if w in nouns:  # nearest label word wins, the word after first ("confidence: Medium", "मध्यम भरोसा")
            for j in (i + 1, i - 1, i + 2, i - 2, i + 3, i - 3):
                if 0 <= j < len(words) and words[j] in label_of:
                    return label_of[words[j]]
    return None


def _conf_as_pct(text: str) -> bool:
    words = "|".join(map(re.escape, load_yaml("copilot")["confidence"]["pct_words"]))
    t = numbers.normalise(text).lower()
    pct = r"\d+(?:\.\d+)?\s*(?:%|percent|pratishat|प्रतिशत)"
    glue = r"(?:\s+(?:of|is|at|level|about|around|roughly|approximately|lagbhag|kareeb|hai|है|का|की|लगभग|करीब))*"
    return bool(re.search(rf"{pct}\s*(?:{words})", t) or re.search(rf"(?:{words}){glue}\s*[:=\-–—]?\s*{pct}", t))


def _stated(e: Entry, text: str, resolved: set[str], language: str) -> bool:
    """An assumption is stated by its number (resolved to it) or, for flags and shares, by one of its phrases."""
    if e.id in resolved:
        return True
    low = numbers.normalise(text).lower()
    return any(p.lower() in low for lang in {language, "en"} for p in e.phrases.get(lang, []))


def validate(candidate: object, reg: Registry, language: str) -> Verdict:
    errs = _schema(candidate)
    if errs:
        return Verdict(False, errs)
    assert isinstance(candidate, dict)
    if candidate["language"] != language:
        errs.append(Issue(None, "LANG_MISMATCH", f"the user wrote in {language}; reply with language '{language}'"))
    sts = candidate["statements"]
    texts = " ".join(st["text"] for st in sts)
    if language == "hinglish" and hinglish_hits(texts) < 2:
        errs.append(Issue(None, "LANG_MISMATCH", "reply in Hinglish (Hindi words in Latin script), as the user wrote"))
    if language == "en" and hinglish_hits(texts) > 1:
        errs.append(Issue(None, "LANG_MISMATCH", "reply in English, as the user wrote"))

    rec_statements: list[tuple[int, dict, set[str]]] = []  # (index, statement, entry ids its numbers resolved to)
    for i, st in enumerate(sts, 1):
        text, refs, label = st["text"], st["refs"], st["label"]
        if not script_fits(text, language):
            want = "Devanagari script" if language == "hi" else "Latin script"
            errs.append(Issue(i, "LANG_MISMATCH", f"write this statement in {want}"))
        for cls, hit in find_regex_pii(_for_numbers(text)):
            errs.append(Issue(i, "PII_LEAK", f"looks like a {cls} ('{hit}'); never include personal identifiers"))
        for w in numbers.number_words(text):
            errs.append(Issue(i, "NUM_WORDS", f"'{w}': write numbers as digits, copied from a display string"))
        if _conf_as_pct(text):
            errs.append(Issue(i, "CONF_AS_PCT", "never give confidence as a percentage; state the label "
                                                "(High/Medium/Low), optionally with the reason line"))
        unknown = [r for r in refs if reg.get(r) is None]
        for r in unknown:
            errs.append(Issue(i, "REF_UNKNOWN", f"'{r}' is not a registry id from this turn"))
        resolved: set[str] = set()
        for n in numbers.extract(_for_numbers(text)):
            matches = reg.matches(n)
            if not matches:
                errs.append(Issue(i, "NUM_UNMATCHED", f"'{n.text.strip()}' is not in this turn's registry; copy "
                                                      f"numbers exactly from display strings{_hint(n, reg, language)}"))
                continue
            cited = [e for e in matches if e.id in refs]
            if not cited:
                ids = ", ".join(f"{e.id} ({e.kind}: {e.desc})" for e in matches[:4])
                errs.append(Issue(i, "REF_MISSING", f"'{n.text.strip()}' matches {ids}; list the id in refs"))
                continue
            resolved.update(e.id for e in cited)
        for r in refs:
            e = reg.get(r)
            if e is not None and e.kind not in ALLOWED_KINDS[label]:
                allowed = ", ".join(sorted(ALLOWED_KINDS[label]))
                errs.append(Issue(i, "LABEL_KIND", f"{r} is a {e.kind} ({e.desc}); a {label} may only cite {allowed}"
                                  + (". Projected and simulated numbers are RECOMMENDATIONs" if e.kind ==
                                     "recommendation" else "")
                                  + (". Forecast numbers are PREDICTIONs" if e.kind == "prediction" else "")))
        if label == "PREDICTION":
            want = (reg.confidence or {}).get("label")
            said = _confidence_label_stated(text, language)
            if want is None:
                errs.append(Issue(i, "PRED_NO_CONF", "no forecast confidence in this turn; call `forecast` first"))
            elif said is None:
                errs.append(Issue(i, "PRED_NO_CONF", f"state the forecast's confidence label: {want} confidence"))
            elif said != want:
                errs.append(Issue(i, "PRED_NO_CONF", f"the forecast's confidence is {want}, not {said}"))
        if label == "RECOMMENDATION":
            if not any(reg.entries[x].kind == "recommendation" for x in resolved):
                errs.append(Issue(i, "REC_NO_IMPACT", "a RECOMMENDATION must state a simulated impact: a number "
                                                      "from a recommendation-kind entry (R…), cited in refs"))
            rec_statements.append((i, st, resolved))

    # Phase 5 item 8: a RECOMMENDATION quoting a simulated number states (and cites) that simulation's assumptions
    for i, st, resolved in rec_statements:
        groups = {reg.entries[x].group for x in resolved if reg.entries[x].kind == "recommendation"} - {None}
        for a in reg.assumptions():
            if a.group in groups and not (a.id in st["refs"] and _stated(a, st["text"], resolved, language)):
                how = f"'{a.display(language)}'" if a.numeric else "in words"
                errs.append(Issue(i, "ASSUMPTION_NOT_CITED",
                                  f"this simulation assumes: {a.desc}. State it here ({how}) and cite {a.id} in refs"))
    return Verdict(not errs, errs)
