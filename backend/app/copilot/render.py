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
SLOT = re.compile(r"\{([=@]?)(\.?[\w.]+)\}")
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


def _confidence(ctx: ToolContext, form: str, refs: list[str]) -> str | None:
    conf = ctx.reg.confidence
    if not conf:
        return None
    t = templates(ctx.language)["confidence"]
    label = _names()["confidence_labels"][conf["label"]][ctx.language]
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
            val = _confidence(ctx, "full" if key == "confidence" else "short", refs)
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
    """Phase 5 item 8: a RECOMMENDATION states every assumption of the simulation whose numbers it quotes."""
    t = templates(ctx.language)
    groups = {ctx.reg.entries[r].group for r in refs if ctx.reg.entries[r].kind == "recommendation"} - {None}
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
    if rtype == "auto_sweep" and not ctx.texts.get(f"{prefix}.to_card", False):
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
        filled = None
        if "rec" in spec or "tradeoff" in spec:
            prefix = "rec.1" if spec.get("rec") == "top" else target or "rec.1"
            if f"{prefix}.type" not in ctx.texts:
                continue
            table = t["recs"] if "rec" in spec else t["tradeoffs"]
            for variant in _rec_variants(ctx, prefix, table):
                filled = _fill(variant, ctx, prefix)
                if filled is not None:
                    break
        else:
            filled = _fill(spec["text"], ctx, None)
        if filled is None:
            continue
        text, refs = filled
        if spec["label"] == "RECOMMENDATION":
            text, refs = _with_assumptions(ctx, text, refs)
        if text[:1].isascii() and text[:1].islower():
            text = text[0].upper() + text[1:]
        statements.append({"label": spec["label"], "text": text, "refs": list(dict.fromkeys(refs))})
    return statements
