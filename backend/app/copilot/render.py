"""Deterministic answers from templates (COPILOT.md §9): used when LLM_PROVIDER=none and as the fallback.

Templates only fill slots from the turn's registry (numbers, cited in refs) and text slots the tools collected, and
the result goes through the same validator as an LLM answer.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from app.copilot.tools import ToolContext
from app.core.config import load_yaml

TEMPLATE_DIR = Path(__file__).parent / "templates"
SLOT = re.compile(r"\{([=@]?)(\.?[\w.:]+)\}")
BAND_KEYS = ("band", "change.band_to", "change.band_from")


@lru_cache
def templates(language: str) -> dict:
    with open(TEMPLATE_DIR / f"{language}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _names() -> dict:
    return load_yaml("copilot_names")


def _key(key: str, prefix: str | None) -> str:
    return f"{prefix}{key}" if key.startswith(".") and prefix else key


def _text_slot(ctx: ToolContext, key: str) -> str | None:
    lang, names = ctx.language, _names()
    v = ctx.texts.get(key)
    if v is None or v == [] or v == "":
        return None
    if key.endswith(".category"):
        return names["categories"].get(v, {}).get(lang) or str(v).replace("_", " ")
    if key in BAND_KEYS:
        return names["bands"].get(v, {}).get(lang, v)
    if key == "top_drag":
        return names["pillars"].get(ctx.texts.get("top_drag.key"), {}).get(lang, v)
    if key == "change.reasons":
        texts = [names["reasons"][c][lang] for c in v if c in names["reasons"]]
        return names["joiners"]["reasons"][lang].join(texts) or None
    if isinstance(v, list):
        items = [str(x) for x in v]
        if len(items) == 1:
            return items[0]
        return names["joiners"]["list"][lang].join(items[:-1]) + names["joiners"]["and"][lang] + items[-1]
    if isinstance(v, bool):
        return "" if v else None
    return str(v)


def _confidence(ctx: ToolContext, form: str, refs: list[str], prefix: str | None = None) -> str | None:
    """The forecast's confidence ({@confidence} full, {@label} short), or a simulation's own label ({@label:<prefix>},
    {@label:.} for the statement's prefix)."""
    if prefix is not None:
        g = ctx.texts.get(f"{prefix}.group")
        raw = ctx.reg.group_confidence.get(g) if g else None
        form = "short"
    else:
        raw = (ctx.reg.confidence or {}).get("label")
    if not raw:
        return None
    t = templates(ctx.language)["confidence"]
    label = _names()["confidence_labels"][raw][ctx.language]
    if form == "full":
        filled = _fill(t["full"].replace("{label}", label), ctx, None)
        if filled is not None:
            text, extra = filled
            refs.extend(extra)
            return text
    return t["short"].replace("{label}", label)


def _fill(text: str, ctx: ToolContext, prefix: str | None) -> tuple[str, list[str]] | None:
    """Fill every slot, or None if any slot is missing (the statement is then skipped)."""
    refs: list[str] = []
    out, pos = [], 0
    for m in SLOT.finditer(text):
        sigil, key = m.group(1), _key(m.group(2), prefix)
        if sigil == "@":
            name, _, of = m.group(2).partition(":")
            of = (prefix if of == "." else of) or None
            val = _confidence(ctx, "full" if name == "confidence" else "short", refs, of)
        elif sigil == "=":
            val = _text_slot(ctx, key)
        else:
            e = ctx.reg.by_key.get(key)
            val = e.display(ctx.language) if e is not None else None
            if e is not None:
                refs.append(e.id)
        if val is None:
            return None
        out.append(text[pos:m.start()] + val)
        pos = m.end()
    out.append(text[pos:])
    return " ".join("".join(out).split()), list(dict.fromkeys(refs))


def _truthy(ctx: ToolContext, cond: str) -> bool:
    if cond.startswith("="):
        return bool(ctx.texts.get(cond[1:]))
    e = ctx.reg.by_key.get(cond)
    return e is not None and (not e.numeric or e.value != 0)


def _with_assumptions(ctx: ToolContext, text: str, refs: list[str]) -> tuple[str, list[str]]:
    """Phase 5 item 8: a statement quoting a simulation's numbers (a RECOMMENDATION, or a what-if's conditional
    PREDICTION) states every assumption of that simulation."""
    t = templates(ctx.language)
    groups = {ctx.reg.entries[r].group for r in refs
              if ctx.reg.entries[r].kind in ("recommendation", "prediction")} - {None}
    items = []
    for a in ctx.reg.assumptions():
        if a.group not in groups:
            continue
        kind = ctx.texts.get(f"{a.key}.key", "tenure" if a.key and a.key.endswith(".tenure") else None)
        phrase = t["assumptions"].get(kind)
        if phrase is None:
            continue
        items.append(phrase.replace("{value}", a.display(ctx.language)))
        refs.append(a.id)
    if items:
        text = f"{text} {t['assumes'].format(items='; '.join(items))}"
    return text, refs


def _rec_variants(ctx: ToolContext, prefix: str, table: dict) -> list[str]:
    rtype = ctx.texts[f"{prefix}.type"]
    keys = []
    if rtype == "pay_down_card" and not ctx.texts.get(f"{prefix}.clears", True):
        keys.append("pay_down_card.partial")
    if rtype == "auto_sweep":
        to_card = ctx.texts.get(f"{prefix}.to_card", False)
        if ctx.texts.get(f"{prefix}.per_payout"):  # irregular income: a share of each payout (fix 6)
            keys.append("auto_sweep.payout" if to_card else "auto_sweep.savings.payout")
        if not to_card:
            keys.append("auto_sweep.savings")
    keys.append(rtype)
    if f"{prefix}.score_delta" not in ctx.reg.by_key:  # the score doesn't move: lead with the money instead
        keys = [f"{k}.flat" for k in keys] + keys
    keys.append(f"{rtype}.fallback")
    return [table[k] for k in keys if k in table]


def render(intent: str, ctx: ToolContext, target: str | None = None) -> list[dict]:
    """Statements for an intent. `target` is the recommendation prefix a trade-off question is about."""
    t = templates(ctx.language)
    statements = []
    for spec in t["intents"][intent]:
        if "when" in spec and not _truthy(ctx, spec["when"]):
            continue
        if "unless" in spec and _truthy(ctx, spec["unless"]):
            continue
        if "rec" in spec or "tradeoff" in spec:
            prefix = "rec.1" if spec.get("rec") == "top" else target or "rec.1"
            st = render_rec(ctx, prefix, "recs" if "rec" in spec else "tradeoffs")
            if st is not None:
                statements.append(st)
            continue
        filled = _fill(spec["text"], ctx, None)
        if filled is None:
            continue
        statements.append(_statement(ctx, spec["label"], *filled))
    return statements


def render_rec(ctx: ToolContext, prefix: str, table_name: str = "recs") -> dict | None:
    """One simulation described by the tools under `prefix` (rec.1, offer.1, sim.new_emi …) as a statement.
    D10: an action Hisaab proposes is a RECOMMENDATION; a what-if is a conditional PREDICTION."""
    if f"{prefix}.type" not in ctx.texts:
        return None
    label = "RECOMMENDATION" if ctx.texts.get(f"{prefix}.proposed", True) else "PREDICTION"
    for variant in _rec_variants(ctx, prefix, templates(ctx.language)[table_name]):
        filled = _fill(variant, ctx, prefix)
        if filled is not None:
            return _statement(ctx, label, *filled)
    return None


def _statement(ctx: ToolContext, label: str, text: str, refs: list[str]) -> dict:
    if label in ("RECOMMENDATION", "PREDICTION"):
        text, refs = _with_assumptions(ctx, text, refs)
    if text[:1].isascii() and text[:1].islower():
        text = text[0].upper() + text[1:]
    return {"label": label, "text": text, "refs": list(dict.fromkeys(refs))}
