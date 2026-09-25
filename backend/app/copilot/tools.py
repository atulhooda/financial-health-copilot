"""Copilot tools (COPILOT.md §4). Read-only and scoped to the turn's user (never an LLM argument).

They read the latest snapshot; `simulate_action` also runs the simulator for what-ifs. Every number leaves as a
registry ref {id, kind, display, desc}: never a raw value, so the validator can trace every number in an answer.
Names that came from bank data (merchants, payees) are untrusted and pass through `sanitize_untrusted`.
Templates read the same registry by `key`, plus the text slots collected in `ToolContext.texts`.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from app.copilot.masking import sanitize_untrusted
from app.copilot.registry import Entry, Registry
from app.core.config import load_yaml

AUTO_TYPES = ("pay_down_card", "redirect_sweep", "auto_sweep", "cancel_overlapping_subs", "link_account")
WHAT_IF_TYPES = ("new_emi", "change_emi_tenure", "delay_purchase")
STANDARD_TENURES = (3, 6, 9, 12, 18, 24, 36)  # tenures a what-if may use without the user naming one
DEFAULT_TENURE = 12
PROB_SHOWN = {"over": {"en": "over 99%", "hi": "99% से ज़्यादा", "hinglish": "99% se zyada"},
              "under": {"en": "under 1%", "hi": "1% से कम", "hinglish": "1% se kam"}}


@dataclass
class ToolContext:
    user_id: str
    language: str
    payload: dict  # the latest snapshot payload
    diff: dict | None  # the diff into the latest snapshot, if any
    reg: Registry
    simulator: Callable[[str, dict], dict | None] | None = None  # (what-if type, params) -> evaluated rec (JSON-able)
    texts: dict[str, object] = field(default_factory=dict)  # non-numeric template slots, by key

    def ref(self, e: Entry) -> dict:
        return self.reg.ref(e, self.language)


class ToolError(Exception):
    """A tool call the orchestrator rejects; its message goes back to the LLM as an error tool result."""


# ---- registering numbers ---------------------------------------------------------------------------------------
def _inr(ctx: ToolContext, kind: str, paise: int | None, desc: str, key: str | None = None,
         group: str | None = None) -> dict | None:
    if paise is None:
        return None
    return ctx.ref(ctx.reg.add(kind, "inr", int(paise), desc, group, key))


def _pct(ctx: ToolContext, kind: str, value: float | None, desc: str, key: str | None = None,
         group: str | None = None, fraction: bool = True) -> dict | None:
    if value is None:
        return None
    v = Decimal(str(round(value * 100 if fraction else value, 4)))
    return ctx.ref(ctx.reg.add(kind, "pct", v, desc, group, key))


def _prob(ctx: ToolContext, kind: str, p: float | None, desc: str, key: str | None = None,
          group: str | None = None) -> dict | None:
    """A Monte Carlo probability, shown as a whole percent; never 0% or 100% ('under 1%', 'over 99%')."""
    if p is None:
        return None
    if p >= 0.995:
        e = ctx.reg.add(kind, "pct", 99, desc, group, key, shown=PROB_SHOWN["over"])
    elif p < 0.005:
        e = ctx.reg.add(kind, "pct", 1, desc, group, key, shown=PROB_SHOWN["under"])
    else:
        e = ctx.reg.add(kind, "pct", round(p * 100), desc, group, key)
    return ctx.ref(e)


def _num(ctx: ToolContext, kind: str, unit: str, value, desc: str, key: str | None = None,
         group: str | None = None) -> dict | None:
    if value is None:
        return None
    if unit == "date" and isinstance(value, str):
        value = dt.date.fromisoformat(value)
    if unit == "months" and not isinstance(value, int):
        value = Decimal(str(round(value, 1)))
    return ctx.ref(ctx.reg.add(kind, unit, value, desc, group, key))


# ---- names from bank data are untrusted --------------------------------------------------------------------------
CONTACT = re.compile(r"(?i)\bcontact[ _](\d+)\b")
ACCT_DIGITS = re.compile(r"\s*\(?(?:••|XX|\*\*)\d{2,6}\)?")


def safe_name(name: str | None, merchant_key: str | None = None) -> str:
    """Payees become [CONTACT_nn] tokens (re-hydrated locally after validation); account digits are dropped."""
    if merchant_key and merchant_key.startswith("p2p:"):
        return f"[{merchant_key[4:].upper()}]"
    if merchant_key == "m:self":
        return "Transfer to your savings"
    n = CONTACT.sub(lambda m: f"__CONTACT_{m.group(1)}__", name or "")
    n = ACCT_DIGITS.sub("", n)
    n = sanitize_untrusted(n)
    return re.sub(r"__CONTACT_(\d+)__", r"[CONTACT_\1]", n)


def _names_by_key(ctx: ToolContext) -> dict[str, str]:
    return {r["merchant_key"]: safe_name(r["merchant_name"], r["merchant_key"]) for r in ctx.payload["recurring"]}


# ---- get_metrics ----------------------------------------------------------------------------------------------
def get_metrics(ctx: ToolContext, args: dict) -> dict:
    p, m, sc = ctx.payload, ctx.payload["metrics"], ctx.payload["score"]
    ctx.texts["band"] = sc["band"]
    pillars = []
    for pl in sc["pillars"]:
        unit, value = pl["unit"], pl["value"]
        val = None
        if value is not None:
            if unit == "months":
                val = _num(ctx, "fact", "months", value, f"{pl['title']}: months of essential spend covered",
                           f"pillar.{pl['key']}.value")
            elif unit == "ratio":
                val = _pct(ctx, "fact", value, f"{pl['title']}: last 30 days' discretionary spend vs usual",
                           f"pillar.{pl['key']}.value")
            else:
                val = _pct(ctx, "fact", value, f"{pl['title']} measure", f"pillar.{pl['key']}.value", fraction=False)
        pillars.append({"name": pl["title"], "status": pl["status"], "note": pl.get("reason"), "value": val,
                        "points": _num(ctx, "fact", "count", pl["contribution"], f"{pl['title']}: points earned",
                                       f"pillar.{pl['key']}.points"),
                        "max_points": _num(ctx, "fact", "count", pl["weight"], f"{pl['title']}: points available",
                                           f"pillar.{pl['key']}.max")})
    drag = next((pl for pl in sc["pillars"] if pl["key"] == sc.get("top_drag")), None)
    ctx.texts["top_drag"] = drag["title"] if drag else None
    ctx.texts["top_drag.key"] = drag["key"] if drag else None
    cov = m["coverage"]
    unlinked = [{"kind": a["kind"], "institution": a["institution"].upper()} for a in p["accounts"]
                if a["status"] != "linked"]
    ctx.texts["unlinked"] = unlinked
    return {
        "as_of": _num(ctx, "fact", "date", p["as_of"], "date of the data this answer uses", "as_of"),
        "score": _num(ctx, "fact", "score", sc["total"], "health score today, out of 100", "score"),
        "band": sc["band"],
        "top_drag": ctx.texts["top_drag"],
        "pillars": pillars,
        "income_monthly": _inr(ctx, "fact", m["income_monthly_paise"], "monthly income (current salary level + usual "
                               "other income)" if m["income_pattern"] != "irregular" else
                               "monthly income (median of the last 3 months; income is irregular)", "income_monthly"),
        "income_pattern": m["income_pattern"],
        "spend_monthly": _inr(ctx, "fact", m["spend_monthly_paise"], "monthly spending incl. EMIs and fees (median of "
                              "recent cycles)", "spend_monthly"),
        "savings_rate": _pct(ctx, "fact", m["savings_rate"], "share of income saved", "savings_rate"),
        "emergency_buffer_months": _num(ctx, "fact", "months", m["buffer_months"], "months of essential spending your "
                                        "own money covers (card debt and new loan cash excluded)", "buffer_months"),
        "emi_monthly": _inr(ctx, "fact", m["emi_monthly_paise"], "total EMIs a month", "emi_monthly"),
        "emi_to_income": _pct(ctx, "fact", m["emi_to_income"], "EMIs as a share of income", "emi_to_income"),
        "debt_to_income": _pct(ctx, "fact", m["debt_to_income"], "total debt as a share of a year's income",
                               "debt_to_income"),
        "credit_utilisation": _pct(ctx, "fact", m["credit_utilisation"], "latest card statement vs card limit",
                                   "credit_utilisation"),
        "revolving_card_balance": _inr(ctx, "fact", m["revolving_paise"], "card balance carried over unpaid (charged "
                                       "interest)", "revolving") if m["revolving_paise"] is not None else
        "unknown: card statement not linked",
        "liquid_balance": _inr(ctx, "fact", m["liquid_paise"], "money in your bank accounts today", "liquid"),
        "new_loan_cash_set_aside": _inr(ctx, "fact", m["earmarked_loan_paise"] or None, "recent loan money, left out "
                                        "of your buffer and forecast", "earmarked"),
        "safety_floor": _inr(ctx, "fact", p["floor_paise"], "safety floor (minimum balance) on your salary account",
                             "floor"),
        "accounts": {"linked": _num(ctx, "fact", "count", cov["accounts_linked"], "accounts linked", "accounts_linked"),
                     "known": _num(ctx, "fact", "count", cov["accounts_known"], "accounts we know of", "accounts_known"),
                     "not_linked": unlinked},
    }


# ---- get_spending ---------------------------------------------------------------------------------------------
def get_spending(ctx: ToolContext, args: dict) -> dict:
    period = args.get("period", "last_30d")
    if period not in ("last_30d", "trailing_3"):
        raise ToolError("period must be 'last_30d' or 'trailing_3'")
    top_n = max(1, min(10, int(args.get("top_n", 6))))
    names = load_yaml("copilot_names")["categories"]
    drift = {d["category"]: d for d in ctx.payload["drift"]}
    rows = [r for r in ctx.payload["spend_by_category"] if r["last_30d_paise"] > 0 or r["trailing_median_paise"] > 0]
    field_ = "last_30d_paise" if period == "last_30d" else "trailing_median_paise"
    rows.sort(key=lambda r: (-r[field_], r["category"]))
    total = sum(r["last_30d_paise"] for r in ctx.payload["spend_by_category"])
    cats = []
    for i, r in enumerate(rows[:top_n], 1):
        c = r["category"]
        label = names.get(c, {}).get("en", c.replace("_", " "))
        ctx.texts[f"spend.{i}.category"] = c
        d = drift.get(c)
        cats.append({
            "category": label,
            "amount": _inr(ctx, "fact", r[field_], f"{label}: spend in the last 30 days" if period == "last_30d" else
                           f"{label}: usual month (median of recent cycles)", f"spend.{i}.amount"),
            "share_of_spend": _pct(ctx, "fact", r["share_last_30d"], f"{label}: share of last 30 days' spend",
                                   f"spend.{i}.share") if period == "last_30d" else None,
            "usual_month": _inr(ctx, "fact", r["trailing_median_paise"], f"{label}: usual month (median of recent "
                                "cycles)", f"spend.{i}.usual") if period == "last_30d" else None,
            "drifting": d is not None,
            "above_usual": _pct(ctx, "fact", d["change_pct"], f"{label}: last 30 days above the usual month",
                                f"spend.{i}.above_usual", fraction=False) if d else None,
        })
    first_drift = next((i for i, x in enumerate(cats, 1) if x["drifting"]), None)
    if first_drift and period == "last_30d":  # template aliases for the first drifting category
        ctx.texts["drift.category"] = ctx.texts[f"spend.{first_drift}.category"]
        for alias, key in (("above_usual", "above_usual"), ("amount", "amount"), ("usual", "usual")):
            ctx.reg.by_key[f"drift.{alias}"] = ctx.reg.by_key[f"spend.{first_drift}.{key}"]
    out = {"period": period, "categories": cats}
    if period == "last_30d":
        out["total_last_30d"] = _inr(ctx, "fact", total, "total spend in the last 30 days", "spend.total")
        merchants = []
        for j, mrow in enumerate(ctx.payload.get("top_merchants_30d", [])[:5], 1):
            nm = safe_name(mrow["name"], mrow["merchant_key"])
            ctx.texts[f"merchant.{j}.name"] = nm
            merchants.append({"merchant": nm, "amount": _inr(ctx, "fact", mrow["amount_paise"],
                                                            f"{nm}: spend in the last 30 days", f"merchant.{j}.amount")})
        out["top_merchants"] = merchants
    return out


# ---- get_recurring --------------------------------------------------------------------------------------------
KIND_FILTER = {"income": ("salary", "income"), "emi": ("emi",), "sip": ("sip",), "rent": ("rent",),
               "subscription": ("subscription",)}


def get_recurring(ctx: ToolContext, args: dict) -> dict:
    kind = args.get("kind", "all")
    if kind != "all" and kind not in KIND_FILTER:
        raise ToolError(f"kind must be one of {['all', *KIND_FILTER]}")
    items = []
    for r in ctx.payload["recurring"]:
        if not r["active"] or (kind != "all" and r["kind"] not in KIND_FILTER[kind]):
            continue
        nm = safe_name(r["merchant_name"], r["merchant_key"])
        items.append({"name": nm, "kind": r["kind"], "cadence": r["cadence"], "direction": r["direction"],
                      "amount": _inr(ctx, "fact", r["amount_paise"], f"{nm}: amount per {r['cadence']} charge"
                                     + (" (varies)" if r["amount_variable"] else ""), f"recurring.{r['merchant_key']}"),
                      "next_due": _num(ctx, "fact", "date", r["next_due"], f"{nm}: next due date",
                                       f"recurring.{r['merchant_key']}.next_due")})
    names = _names_by_key(ctx)
    overlaps = []
    for o in ctx.payload["overlaps"]:
        members = [names.get(k, sanitize_untrusted(k)) for k in o["merchant_keys"]]
        ctx.texts[f"overlap.{o['group']}.members"] = members
        overlaps.append({"group": o["group"], "members": members,
                         "count": _num(ctx, "fact", "count", o["count"], f"overlapping {o['group']} subscriptions",
                                       f"overlap.{o['group']}.count"),
                         "monthly_total": _inr(ctx, "fact", o["monthly_total_paise"], f"{o['group']} subscriptions: "
                                               "total a month", f"overlap.{o['group']}.total")})
    return {"items": items, "overlaps": overlaps}


# ---- forecast -------------------------------------------------------------------------------------------------
def _confidence(ctx: ToolContext) -> dict:
    c = ctx.payload["confidence"]
    ctx.reg.confidence = {"label": c["label"], "reason": c["reason"]}
    caps = []
    for cap in c.get("caps") or []:
        m = re.match(r"(\d+) of (\d+) accounts not linked|(\d+) accounts? not linked|only ([\d.]+) months of history",
                     cap)
        if m and m.group(3):
            caps.append({"why": "accounts not linked", "count": _num(ctx, "fact", "count", int(m.group(3)),
                                                                     "accounts not linked", "conf.cap.unlinked")})
        elif m and m.group(1):
            caps.append({"why": "accounts not linked", "count": _num(ctx, "fact", "count", int(m.group(1)),
                                                                     "accounts not linked", "conf.cap.unlinked")})
        elif m and m.group(4):
            caps.append({"why": "short history", "months": _num(ctx, "fact", "months", float(m.group(4)),
                                                                 "months of history", "conf.cap.history")})
    ctx.texts["confidence.label"] = c["label"]
    ctx.texts["confidence.capped"] = bool(caps)
    return {"label": c["label"],
            "band_held_on_past_days": _pct(ctx, "fact", c["coverage"], "share of past days the forecast's 10-90% band "
                                           "held when replayed (backtest)", "conf.coverage"),
            "target": _pct(ctx, "fact", c["target"], "the band's target hit rate", "conf.target"),
            "capped_by": caps,
            "how_to_say_it": "state the label, optionally with the reason line (band held on X of past days, target "
                             "Y); never as a percentage of confidence"}


def forecast(ctx: ToolContext, args: dict) -> dict:
    fc = ctx.payload["forecast"]
    if not fc.get("available"):
        return {"available": False, "reason": fc.get("reason") or "not enough history"}
    salaried = fc.get("income_basis") == "salary"
    ctx.texts["forecast.salaried"] = salaried
    out: dict = {
        "available": True,
        "account": "your salary account" if salaried else "your main account",
        "horizon_days": _num(ctx, "fact", "days", fc["horizon_days"], "forecast horizon", "forecast.horizon"),
        "next_income_date": _num(ctx, "fact" if salaried else "prediction", "date", fc["next_income_date"],
                                 "next salary date" if salaried else "next income date (irregular income: assumed)",
                                 "forecast.next_income"),
        "safety_floor": _inr(ctx, "fact", fc["floor_paise"], "safety floor (minimum balance)", "floor"),
        "chance_of_dipping_below_floor_before_next_income": _prob(
            ctx, "prediction", fc["dip_probability"], "chance the balance dips below the safety floor before the next "
            "income", "forecast.dip"),
        "dip_metric_saturated": bool(fc.get("dip_saturated")),
        "likely_dip_date": _num(ctx, "prediction", "date", fc.get("likely_dip_date"), "most likely date of the first "
                                "dip below the floor", "forecast.dip_date"),
        "lowest_balance_next_30_days": _inr(ctx, "prediction", fc.get("projected_low_paise"), "lowest balance in the "
                                            "next 30 days (median path)", "forecast.low"),
    }
    pre = fc.get("pre_income_p10_p50_p90")
    if pre:
        out["balance_day_before_next_income"] = {
            "low_p10": _inr(ctx, "prediction", pre[0], "balance the day before next income: low case (P10)",
                            "forecast.pre.p10"),
            "middle_p50": _inr(ctx, "prediction", pre[1], "balance the day before next income: middle case (P50)",
                               "forecast.pre.p50"),
            "high_p90": _inr(ctx, "prediction", pre[2], "balance the day before next income: high case (P90)",
                             "forecast.pre.p90")}
    risks = sorted(fc.get("bounce_risks") or [], key=lambda b: (-b["probability"], b["due_date"], b["name"]))
    mandates = [b for b in risks if b["mandate"]][:2]
    others = [b for b in risks if not b["mandate"]][:2]
    for tag, group in (("m", mandates), ("o", others)):
        rows = []
        for i, b in enumerate(group, 1):
            nm = safe_name(b["name"], b.get("merchant_key") if b.get("merchant_key", "").startswith("p2p:") else None)
            k = f"bounce.{tag}{i}"
            ctx.texts[f"{k}.name"] = nm
            rows.append({"name": nm, "kind": b["kind"],
                         "due_date": _num(ctx, "fact", "date", b["due_date"], f"{nm}: due date", f"{k}.due"),
                         "amount": _inr(ctx, "fact", b["amount_paise"], f"{nm}: amount due", f"{k}.amount"),
                         "chance_it_bounces": _prob(ctx, "prediction", b["probability"], f"chance the {nm} payment "
                                                    "finds too little money in the account", f"{k}.probability")})
        out["emi_and_sip_bounce_risks" if tag == "m" else "other_bounce_risks"] = rows
    assumptions = []
    for a in fc.get("assumptions") or []:
        if a.get("key") == "loan_cash_earmarked":
            ctx.texts["forecast.earmark"] = True
            assumptions.append({
                "assumes": "the recent loan money goes to its purpose, so it is left out of the forecast",
                "amount": _inr(ctx, "fact", a["amount_paise"], "recent loan money left out of the forecast",
                               "forecast.earmark.amount"),
                "if_you_keep_it_chance_of_dip": _prob(ctx, "prediction", (a.get("alternative") or {}).get(
                    "dip_probability_if_kept"), "chance of dipping below the floor if you keep the loan money",
                    "forecast.earmark.dip_if_kept")})
    out["assumptions"] = assumptions
    out["confidence"] = _confidence(ctx)
    return out


# ---- recommendations ------------------------------------------------------------------------------------------
ASSUMPTION_DESC = {
    "card_rate": {"statement": "card interest per month, implied by your own statements (finance charges + GST)",
                  "config": "card interest per month: a typical rate + GST (no statement rate available)"},
    "savings_interest": "interest your savings earn per year (assumed)",
    "pay_in_full_after": "once the card is clear you pay the full statement every month (otherwise the balance "
                         "rebuilds)",
    "unswept_spend_share": "half of any freed-up cash gets spent anyway unless it is moved away (assumed)",
    "lender_approval": "the lender must approve; a restructuring fee may apply (not included)",
    "emi_rate": {"user": "loan interest rate per year (you gave it)",
                 "config": "loan interest rate per year (assumed; you didn't give one)"},
}


def _assumption(ctx: ToolContext, a: dict, group: str, key: str) -> dict:
    kind = {"statement": "fact", "user": "user_input"}.get(a["source"], "assumption")
    desc = ASSUMPTION_DESC.get(a["key"], re.sub(r"\s*\(D\d+\w*\)", "", re.sub(r"[\d.,]+%?", "", a["text"])))
    if isinstance(desc, dict):
        desc = desc.get(a["source"], next(iter(desc.values())))
    phrases = load_yaml("copilot")["assumptions"].get(a["key"], {})
    unit = a["unit"]
    if unit == "flag":
        e = ctx.reg.add("assumption", "flag", None, desc, group, key, phrases=phrases)
    elif unit == "share":
        e = ctx.reg.add(kind, "pct", Decimal(str(round(a["value"] * 100, 2))), desc, group, key, phrases=phrases)
    else:  # pct_per_month / pct_per_year
        e = ctx.reg.add(kind, "pct", Decimal(str(a["value"])), desc, group, key, phrases=phrases)
    ctx.texts[f"{key}.key"] = a["key"]
    return {"assumes": desc, **ctx.ref(e)}


def _impact(ctx: ToolContext, imp: dict, g: str, kp: str, kind: str = "recommendation") -> dict:
    """A simulation's numbers: `recommendation`-kind for an action Hisaab proposes, `prediction`-kind for a
    what-if the user asked about (a conditional PREDICTION, D10)."""
    out: dict = {}

    def put(name, value):
        if value is not None:
            out[name] = value

    put("rupee_impact_12_months", _inr(ctx, kind, imp.get("annual_impact_paise"),
                                       "rupee impact over 12 months (interest saved, minus savings interest lost, "
                                       "plus cash freed)", f"{kp}.annual", g))
    if imp.get("monthly_cashflow_paise"):
        put("monthly_cash_flow_change", _inr(ctx, kind, imp["monthly_cashflow_paise"],
                                             "change in monthly cash flow (negative = more going out)",
                                             f"{kp}.monthly", g))
    if imp.get("card_interest_saved_12m_paise"):
        put("card_interest_saved_12_months", _inr(ctx, kind, imp["card_interest_saved_12m_paise"],
                                                  "card interest saved over 12 months", f"{kp}.card_interest_saved", g))
    if imp.get("loan_interest_saved_12m_paise"):
        v = imp["loan_interest_saved_12m_paise"]
        put("loan_interest_change_12_months", _inr(ctx, kind, v, "loan interest saved over 12 months"
                                                   if v > 0 else "extra loan interest over 12 months",
                                                   f"{kp}.loan_interest", g))
    if imp.get("savings_interest_foregone_12m_paise", 0) > 0:
        put("savings_interest_lost_12_months", _inr(ctx, kind, imp["savings_interest_foregone_12m_paise"],
                                                    "savings interest given up over 12 months",
                                                    f"{kp}.savings_interest_lost", g))
    put("score_today", _num(ctx, "fact", "score", imp.get("score_now"), "health score today", f"{kp}.score_now"))
    put("score_in_12_months_without", _num(ctx, kind, "score", imp.get("score_12m_baseline"),
                                           "projected score in 12 months if nothing changes", f"{kp}.score_base", g))
    put("score_in_12_months_with", _num(ctx, kind, "score", imp.get("score_12m_with_action"),
                                        "projected score in 12 months with this", f"{kp}.score_with", g))
    if imp.get("score_delta_12m"):
        put("score_change_12_months", _num(ctx, kind, "score", imp["score_delta_12m"],
                                           "projected score change in 12 months (points)", f"{kp}.score_delta", g))
    b0, b1 = imp.get("bounce_risk_before"), imp.get("bounce_risk_after")
    if b0 is not None and b1 is not None and (b0 or b1):
        put("emi_bounce_risk_before", _prob(ctx, kind, b0, "largest EMI/SIP bounce risk (next 45 days) "
                                            "as things stand", f"{kp}.bounce_before", g))
        put("emi_bounce_risk_after", _prob(ctx, kind, b1, "largest EMI/SIP bounce risk (next 45 days) "
                                           "with this", f"{kp}.bounce_after", g))
    if not imp.get("dip_saturated") and imp.get("dip_probability_before") is not None:
        put("dip_chance_before", _prob(ctx, kind, imp["dip_probability_before"], "chance of dipping below "
                                       "the floor before next income, as things stand", f"{kp}.dip_before", g))
        put("dip_chance_after", _prob(ctx, kind, imp["dip_probability_after"], "chance of dipping below "
                                      "the floor before next income, with this", f"{kp}.dip_after", g))
    if imp.get("buffer_months_now_after") is not None and imp.get("buffer_months_now") is not None and \
            abs(imp["buffer_months_now_after"] - imp["buffer_months_now"]) >= 0.05:
        put("buffer_months_today", _num(ctx, "fact", "months", imp["buffer_months_now"], "emergency buffer today "
                                        "(months)", f"{kp}.buffer_now"))
        put("buffer_months_right_after", _num(ctx, kind, "months", imp["buffer_months_now_after"],
                                              "emergency buffer right after doing this (months)",
                                              f"{kp}.buffer_after", g))
    b12, w12 = imp.get("buffer_months_12m_baseline"), imp.get("buffer_months_12m_with_action")
    if b12 is not None and w12 is not None and abs(w12 - b12) >= 0.05:
        put("buffer_months_in_12_months_without", _num(ctx, kind, "months", b12, "emergency buffer in 12 "
                                                       "months if nothing changes (months)", f"{kp}.buffer_12m_base", g))
        put("buffer_months_in_12_months_with", _num(ctx, kind, "months", w12, "emergency buffer in 12 "
                                                    "months with this (months)", f"{kp}.buffer_12m_with", g))
    if imp.get("months_to_clear_card") is not None:
        put("months_to_clear_card", _num(ctx, kind, "months", imp["months_to_clear_card"],
                                         "months until the card carries no balance", f"{kp}.months_to_clear", g))
    if imp.get("revolving_12m_baseline_paise") is not None and imp.get("revolving_12m_with_action_paise") is not None \
            and imp["revolving_12m_baseline_paise"] != imp["revolving_12m_with_action_paise"]:
        put("card_balance_in_12_months_without", _inr(ctx, kind, imp["revolving_12m_baseline_paise"],
                                                      "unpaid card balance in 12 months if nothing changes",
                                                      f"{kp}.revolving_base", g))
        put("card_balance_in_12_months_with", _inr(ctx, kind, imp["revolving_12m_with_action_paise"],
                                                   "unpaid card balance in 12 months with this",
                                                   f"{kp}.revolving_with", g))
    return out


def describe_rec(ctx: ToolContext, rec: dict, kp: str, proposed: bool = True) -> dict:
    """One simulation, with every number registered in its own group. Label by who proposed the action (D10):
    an action Hisaab proposes gives `recommendation`-kind numbers (a RECOMMENDATION); a what-if the user asked about,
    or one Hisaab only offers to explore, gives `prediction`-kind numbers (a conditional PREDICTION)."""
    for stale in [k for k in ctx.reg.by_key if k.startswith(f"{kp}.")]:  # a later call replaces the prefix whole
        del ctx.reg.by_key[stale]
    for stale in [k for k in ctx.texts if k.startswith(f"{kp}.")]:
        del ctx.texts[stale]
    g = ctx.reg.new_group()
    rk = "recommendation" if proposed else "prediction"
    ctx.reg.group_confidence[g] = rec["confidence"]["label"]
    if not proposed:
        ctx.reg.what_if_groups.add(g)
    t, p, imp = rec["type"], rec["params"], rec["impact"]
    ctx.texts[f"{kp}.type"] = t
    ctx.texts[f"{kp}.group"] = g
    ctx.texts[f"{kp}.proposed"] = proposed
    out: dict = {"rank": rec.get("rank"), "type": t,
                 "label_as": "RECOMMENDATION: an action Hisaab proposes" if proposed else
                 "PREDICTION: a what-if; phrase it with its condition ('If you …'), give its confidence label and "
                 "state its assumptions"}
    if imp.get("kind") == "data":
        inst = (p.get("institution") or "").upper()
        ctx.texts[f"{kp}.institution"] = inst
        out["what"] = f"Link your {inst} card statement so we can see its balance"
        out["impact"] = {
            "accounts_linked_now": _num(ctx, "fact", "count", imp.get("accounts_linked_before"), "accounts linked now",
                                        f"{kp}.linked_before"),
            "accounts_linked_after": _num(ctx, rk, "count", imp.get("accounts_linked_after"),
                                          "accounts linked after this", f"{kp}.linked_after", g),
            "accounts_known": _num(ctx, "fact", "count", imp.get("accounts_known"), "accounts we know of",
                                   f"{kp}.known"),
            "unlocks": imp.get("unlocks", [])}
        out["confidence"] = rec["confidence"]["label"]
        return out
    names = _names_by_key(ctx)
    if t == "pay_down_card":
        amt = _inr(ctx, rk, p["amount_paise"], "savings moved to the card", f"{kp}.amount", g)
        cushion = _inr(ctx, rk, p["cushion_paise"], "cushion kept in savings", f"{kp}.cushion", g)
        out["what"] = (f"Use {amt['display']} of savings to clear your card, then set card autopay to the full "
                       f"statement amount; keep {cushion['display']} as a cushion" if p.get("clears") else
                       f"Use {amt['display']} of savings to pay down your card; keep {cushion['display']} as a cushion")
        out["steps"] = [f"Move {amt['display']} from savings to the card", "Set card autopay to the full statement "
                        "amount"] if p.get("clears") else [f"Move {amt['display']} from savings to the card"]
        out["amount"], out["cushion"] = amt, cushion
        ctx.texts[f"{kp}.clears"] = bool(p.get("clears"))
    elif t == "redirect_sweep":
        amt = _inr(ctx, rk, p["amount_paise"], "your monthly transfer to savings, sent to the card "
                   "instead", f"{kp}.amount", g)
        out["what"] = f"Send your {amt['display']} monthly transfer to the card instead of savings until it's clear"
        out["amount"] = amt
    elif t == "auto_sweep":
        to_card = p.get("target") == "card"
        ctx.texts[f"{kp}.to_card"] = to_card
        if p.get("per") == "payout":  # irregular income: a share of each payout (fix 6)
            ctx.texts[f"{kp}.per_payout"] = True
            share = _pct(ctx, rk, p["share_bps"] / 100, "share of each payout moved", f"{kp}.share", g,
                         fraction=False)
            amt = _inr(ctx, rk, p["amount_paise"], "about this much a month (at your usual income)", f"{kp}.amount", g)
            where = "to the card" if to_card else "to savings"
            out["what"] = f"Move {share['display']} of each payout {where} (about {amt['display']} a month)"
            out["share_of_each_payout"] = share
        else:
            amt = _inr(ctx, rk, p["amount_paise"], "amount moved on each payday", f"{kp}.amount", g)
            out["what"] = (f"On payday, send {amt['display']} extra to the card" if to_card else
                           f"On payday, move {amt['display']} to savings")
        out["amount"] = amt
        out["sized_by"] = {"surplus": "your usual monthly surplus", "bounce_risk": "EMI bounce risk",
                           "dip": "the risk of running low"}.get(p.get("binding"), p.get("binding"))
    elif t == "cancel_overlapping_subs":
        keep = names.get(p["keep"], sanitize_untrusted(p["keep"]))
        cancel = [names.get(k, sanitize_untrusted(k)) for k in p["cancel"]]
        ctx.texts[f"{kp}.keep"], ctx.texts[f"{kp}.cancel"] = keep, cancel
        amt = _inr(ctx, rk, p["monthly_paise"], "saved a month by cancelling", f"{kp}.amount", g)
        out["what"] = f"Keep {keep}; cancel {', '.join(cancel)} ({amt['display']} a month)"
        out["amount"] = amt
    elif t == "change_emi_tenure":
        loan = names.get(p["loan"], sanitize_untrusted(p["loan"]))
        ctx.texts[f"{kp}.loan"] = loan
        extra = _inr(ctx, rk, rec["extra"].get("lifetime_cost_paise"), "extra interest over the loan's "
                     "life", f"{kp}.extra_interest", g)
        months = _num(ctx, rk, "months", p["extra_months"], "months added to the loan",
                      f"{kp}.extra_months", g)
        old = _inr(ctx, "fact", p["old_emi_paise"], f"{loan} EMI today", f"{kp}.old_emi")
        new = _inr(ctx, rk, p["new_emi_paise"], f"{loan} EMI if extended", f"{kp}.new_emi", g)
        out["what"] = (f"Explore (not a recommendation): {extra['display']} more interest to extend your {loan} loan "
                       f"by {months['display']} (EMI {old['display']} → {new['display']}); needs the lender's approval")
        out.update({"extra_interest": extra, "months_added": months, "emi_now": old, "emi_if_extended": new})
    elif t == "new_emi":
        e = rec["extra"]
        out["what"] = "a new EMI (what-if)"
        out["emi"] = _inr(ctx, rk, e["emi_paise"], "the new EMI", f"{kp}.emi", g)
        out["total_interest"] = _inr(ctx, rk, e["total_interest_paise"], "total interest over the loan",
                                     f"{kp}.total_interest", g)
        alt = e.get("alt_tenure")
        if alt:
            out["longer_tenure_option"] = {
                "tenure": _num(ctx, rk, "months", alt["tenure_months"], "longer tenure option",
                               f"{kp}.alt_tenure", g),
                "emi": _inr(ctx, rk, alt["emi_paise"], "EMI with the longer tenure", f"{kp}.alt_emi", g)}
        out["emis_share_of_income_now"] = _pct(ctx, "fact", imp.get("emi_to_income_before"), "EMIs as a share of "
                                               "income today", f"{kp}.eti_before")
        out["emis_share_of_income_after"] = _pct(ctx, rk, imp.get("emi_to_income_after"),
                                                 "EMIs as a share of income with the new EMI", f"{kp}.eti_after", g)
        dates = imp.get("emi_dates") or {}
        dk = "recommendation" if dates.get("advice") else rk  # "ask for an EMI date after payday" is Hisaab's advice
        opts = []
        for o in dates.get("options", []):
            tag = "after_payday" if o["option"] == "just_after_payday" else "in_30_days"
            opts.append({"option": "first EMI just after payday" if tag == "after_payday" else
                         "first EMI 30 days from today",
                         "first_due": _num(ctx, dk, "date", o["first_due"], f"first EMI date ({tag})",
                                           f"{kp}.date.{tag}", g),
                         "chance_new_emi_bounces": _prob(ctx, dk, o["new_emi_bounce_risk"],
                                                         f"chance the new EMI bounces ({tag})",
                                                         f"{kp}.date.{tag}.risk", g)})
        out["first_emi_date_options"] = opts
        if dates.get("advice"):
            ctx.texts[f"{kp}.advice"] = True
            out["advice"] = {"what": "Ask for an EMI date right after your salary date",
                             "label_as": "RECOMMENDATION: Hisaab's advice, with the two dates' bounce risks"}
        top = imp.get("top_bounce_after")
        if top:
            ctx.texts[f"{kp}.top_bounce_name"] = safe_name(top["name"])
            out["largest_emi_bounce_risk_with_it"] = {"name": safe_name(top["name"])}
    elif t == "delay_purchase":
        out["what"] = "Buy after your next payday instead of now"
    out["impact"] = _impact(ctx, imp, g, kp, rk)
    if t == "pay_down_card":
        d = rec.get("extra", {}).get("downside")
        if d:
            out["downside_if_you_go_back_to_paying_part"] = {
                "usual_share_paid": _pct(ctx, "fact", d["pay_share"], "share of the card bill you usually pay",
                                         f"{kp}.pay_share"),
                "balance_rebuilds_to": _inr(ctx, rk, d["rebuild_to_paise"], "unpaid card balance it "
                                            "rebuilds to if you go back to paying the usual share",
                                            f"{kp}.rebuild_to", g),
                "within_months": _num(ctx, rk, "months", d["months_to_rebuild"], "months until it "
                                      "rebuilds", f"{kp}.rebuild_months", g),
                "and_the_savings_are_gone": True}
        chk = rec.get("extra", {}).get("post_clear_check")
        if chk and chk.get("raises"):
            out["cushion_for_paying_in_full"] = {
                "extra_cushion_kept": _inr(ctx, rk, chk["extra_cushion_paise"], "extra cushion kept in "
                                           "savings for the first full bills", f"{kp}.extra_cushion", g),
                "emi_bounce_risk_90_days": _prob(ctx, rk, chk.get("bounce_risk_after_with_cushion",
                                                                                chk["bounce_risk_after"]),
                                                 "largest EMI bounce risk over 90 days when paying in full, with the "
                                                 "cushion", f"{kp}.bounce_90d", g)}
    out["assumptions"] = [_assumption(ctx, a, g, f"{kp}.assume.{i}") for i, a in enumerate(rec["assumptions"], 1)]
    ctx.texts[f"{kp}.assumption_keys"] = [f"{kp}.assume.{i}" for i in range(1, len(rec["assumptions"]) + 1)]
    out["confidence"] = rec["confidence"]["label"]
    if t in WHAT_IF_TYPES:
        out["note"] = "a what-if to explore, not a recommendation"
    return out


def list_recommendations(ctx: ToolContext, args: dict) -> dict:
    limit = max(1, min(5, int(args.get("limit", 3))))
    recs = [describe_rec(ctx, r, f"rec.{i}") for i, r in enumerate(ctx.payload["recommendations"][:limit], 1)]
    out: dict = {"recommendations": recs}
    plan = ctx.payload.get("combined_plan")
    if plan and limit >= 2:
        g = ctx.reg.new_group()
        ctx.reg.group_confidence[g] = plan["confidence"]["label"]
        out["combined_plan"] = {"actions": len(plan["actions"]), "label_as": "RECOMMENDATION: Hisaab's plan",
                                "impact": _impact(ctx, plan["impact"], g, "plan"),
                                "assumptions": [_assumption(ctx, a, g, f"plan.assume.{i}")
                                                for i, a in enumerate(plan["assumptions"], 1)]}
    offers = ctx.payload.get("what_if_offers") or []
    if offers:
        out["what_ifs_to_explore"] = [describe_rec(ctx, o, f"offer.{i}", proposed=False)  # D37: never recommended
                                      for i, o in enumerate(offers[:2], 1)]
    ctx.texts["recs.count"] = len(recs)
    return out


# ---- simulate_action ------------------------------------------------------------------------------------------
def _registered(ctx: ToolContext, unit: str, value: Decimal, kinds: tuple[str, ...] | None = None) -> Entry | None:
    for e in ctx.reg.entries.values():
        if e.unit == unit and e.numeric and e.value == value and (kinds is None or e.kind in kinds):
            return e
    return None


def simulate_action(ctx: ToolContext, args: dict) -> dict:
    action = args.get("action") or {}
    t, params = action.get("type"), action.get("params") or {}
    if t in AUTO_TYPES:
        rec = next((r for r in ctx.payload["recommendations"] if r["type"] == t), None)
        if rec is None:
            return {"available": False, "reason": f"'{t}' doesn't apply to your data right now"}
        return describe_rec(ctx, rec, f"sim.{t}")
    if t == "change_emi_tenure":
        loan = str(params.get("loan", "")).lower()
        offer = next((o for o in ctx.payload.get("what_if_offers") or []
                      if not loan or loan in o["params"]["loan"] or loan in o["title"].lower()), None)
        if offer is None:
            return {"available": False, "reason": "a longer tenure is only explored when an EMI's bounce risk is 20% "
                                                  "or more"}
        return describe_rec(ctx, offer, "sim.change_emi_tenure", proposed=False)
    if t not in WHAT_IF_TYPES:
        raise ToolError(f"unknown action type {t!r}")
    if ctx.simulator is None:
        raise ToolError("simulation is not available")
    if t == "new_emi":
        try:
            principal = Decimal(int(params["principal_rupees"]))
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError("new_emi needs params.principal_rupees (an integer the user gave)") from e
        if _registered(ctx, "inr", principal) is None:
            raise ToolError(f"principal_rupees {principal} is not an amount the user gave or a tool returned")
        tenure = params.get("tenure_months")
        assumed_tenure = tenure is None
        tenure = DEFAULT_TENURE if tenure is None else int(tenure)
        if not assumed_tenure and tenure not in STANDARD_TENURES and _registered(ctx, "months", Decimal(tenure)) is None:
            raise ToolError(f"tenure_months {tenure} is not a tenure the user gave or a standard one")
        rate = params.get("annual_rate_pct")
        if rate is not None and _registered(ctx, "pct", Decimal(str(rate)), ("user_input",)) is None:
            raise ToolError(f"annual_rate_pct {rate} is not a rate the user gave; leave it out to use the assumed rate")
        rec = ctx.simulator("new_emi", {"principal_paise": int(principal * 100), "tenure_months": tenure,
                                        "annual_rate_bps": int(Decimal(str(rate)) * 100) if rate is not None else None})
        if rec is None:
            return {"available": False, "reason": "not enough data to simulate"}
        out = describe_rec(ctx, rec, "sim.new_emi", proposed=False)  # the user's what-if
        g = ctx.texts["sim.new_emi.group"]
        pe = _registered(ctx, "inr", principal)
        ctx.reg.by_key["sim.new_emi.principal"] = pe
        out["principal"] = ctx.ref(pe)
        if assumed_tenure:  # item 8: a tenure the user didn't give is an assumption the answer must state
            te = ctx.reg.add("assumption", "months", tenure, "tenure (assumed; you didn't give one)", g,
                             "sim.new_emi.tenure")
            ctx.texts["sim.new_emi.assumption_keys"].append("sim.new_emi.tenure")
        else:
            te = _registered(ctx, "months", Decimal(tenure), ("user_input",)) or ctx.reg.add(
                "prediction", "months", tenure, "tenure", g)
            ctx.reg.by_key["sim.new_emi.tenure"] = te
        out["tenure"] = ctx.ref(te)
        return out
    try:
        amount = Decimal(int(params["amount_rupees"]))
    except (KeyError, TypeError, ValueError) as e:
        raise ToolError("delay_purchase needs params.amount_rupees (an integer the user gave)") from e
    if _registered(ctx, "inr", amount) is None:
        raise ToolError(f"amount_rupees {amount} is not an amount the user gave or a tool returned")
    rec = ctx.simulator("delay_purchase", {"amount_paise": int(amount * 100)})
    if rec is None:
        return {"available": False, "reason": "not enough data to simulate"}
    return describe_rec(ctx, rec, "sim.delay_purchase", proposed=False)


# ---- explain_change -------------------------------------------------------------------------------------------
def explain_change(ctx: ToolContext, args: dict) -> dict:
    d = ctx.diff
    if not d:
        return {"available": False, "reason": "there is no earlier snapshot to compare with"}
    sc = d["score"]
    ctx.texts["change.band_from"], ctx.texts["change.band_to"] = sc["band_from"], sc["band_to"]
    ctx.texts["change.reasons"] = [c for c in d["reason_codes"] if not c.startswith("REC_")]
    out: dict = {
        "from_date": _num(ctx, "fact", "date", d["from_as_of"], "date of the earlier snapshot", "change.from_date"),
        "to_date": _num(ctx, "fact", "date", d["to_as_of"], "date of the latest snapshot", "change.to_date"),
        "score_before": _num(ctx, "fact", "score", sc["from"], "health score before", "change.score_from"),
        "score_after": _num(ctx, "fact", "score", sc["to"], "health score now", "change.score_to"),
        "points_changed": _num(ctx, "fact", "score", abs(sc["delta"]), "points the score moved"
                               + (" down" if sc["delta"] < 0 else " up"), "change.score_delta"),
        "direction": "down" if sc["delta"] < 0 else "up" if sc["delta"] > 0 else "unchanged",
        "band_before": sc["band_from"], "band_after": sc["band_to"],
        "reason_codes": d["reason_codes"],
    }
    if sc.get("new_data"):
        out["points_from_newly_linked_data"] = _num(ctx, "fact", "score", abs(sc["new_data"]), "points explained by "
                                                    "data we couldn't see before (newly linked accounts)",
                                                    "change.new_data")
        out["points_from_behaviour_and_time"] = _num(ctx, "fact", "score", abs(sc["behaviour"]), "points explained "
                                                     "by what changed in your money", "change.behaviour")
    pillars = []
    for pd in d["pillar_deltas"]:
        if pd["delta"]:
            nm = next((pl["title"] for pl in ctx.payload["score"]["pillars"] if pl["key"] == pd["key"]), pd["key"])
            pillars.append({"pillar": nm,
                            "points_before": _num(ctx, "fact", "count", pd["from"], f"{nm} points before",
                                                  f"change.pillar.{pd['key']}.from"),
                            "points_after": _num(ctx, "fact", "count", pd["to"], f"{nm} points now",
                                                 f"change.pillar.{pd['key']}.to")})
    out["pillars_that_moved"] = pillars
    metric_desc = {"income_monthly_paise": ("inr", "monthly income"), "spend_monthly_paise": ("inr", "monthly spend"),
                   "revolving_paise": ("inr", "unpaid card balance"), "savings_rate": ("pct", "savings rate"),
                   "emi_to_income": ("pct", "EMIs as a share of income"),
                   "credit_utilisation": ("pct", "card utilisation"), "buffer_months": ("months", "emergency buffer")}
    metrics = {}
    for k, ch in d.get("metrics", {}).items():
        if k not in metric_desc or ch["from"] is None or ch["to"] is None:
            continue
        unit, desc = metric_desc[k]
        short = k.replace("_paise", "")
        if unit == "inr":
            metrics[short] = {"before": _inr(ctx, "fact", ch["from"], f"{desc} before", f"change.{short}.from"),
                              "after": _inr(ctx, "fact", ch["to"], f"{desc} now", f"change.{short}.to")}
        elif unit == "pct":
            metrics[short] = {"before": _pct(ctx, "fact", ch["from"], f"{desc} before", f"change.{short}.from"),
                              "after": _pct(ctx, "fact", ch["to"], f"{desc} now", f"change.{short}.to")}
        else:
            metrics[short] = {"before": _num(ctx, "fact", "months", ch["from"], f"{desc} before",
                                             f"change.{short}.from"),
                              "after": _num(ctx, "fact", "months", ch["to"], f"{desc} now", f"change.{short}.to")}
    out["metrics_that_moved"] = metrics
    b = d.get("bounce") or {}
    if b.get("from") is not None and b.get("to") is not None and abs(b["to"] - b["from"]) >= 0.10:
        out["emi_bounce_risk"] = {"before": _prob(ctx, "prediction", b["from"], "largest EMI/SIP bounce risk at the "
                                                  "earlier snapshot", "change.bounce.from"),
                                  "now": _prob(ctx, "prediction", b["to"], "largest EMI/SIP bounce risk now",
                                               "change.bounce.to")}
        _confidence(ctx)
    changes = []
    for rc in d.get("rec_changes", []):
        changes.append({"action": rc["type"], "change": rc["change"],
                        "rank_before": _num(ctx, "fact", "count", rc["from_rank"], "rank before",
                                            f"change.rec.{rc['action_key']}.from"),
                        "rank_now": _num(ctx, "fact", "count", rc["to_rank"], "rank now",
                                         f"change.rec.{rc['action_key']}.to"),
                        "caused_by": rc["caused_by"]})
    out["recommendation_changes"] = changes
    return out


# ---- registry of tools ----------------------------------------------------------------------------------------
TOOLS: dict[str, Callable[[ToolContext, dict], dict]] = {
    "get_metrics": get_metrics, "get_spending": get_spending, "get_recurring": get_recurring, "forecast": forecast,
    "list_recommendations": list_recommendations, "simulate_action": simulate_action,
    "explain_change": explain_change,
}

_OBJ = {"type": "object", "additionalProperties": False}
TOOL_SPECS: list[dict] = [
    {"name": "get_metrics", "description": "The user's health score (0-100) with its pillars, income, spending, "
     "savings rate, emergency buffer, EMIs, debt, card use and linked accounts. All facts.",
     "input_schema": {**_OBJ, "properties": {}}},
    {"name": "get_spending", "description": "Spending by category (last 30 days, or a usual month) with drift vs the "
     "usual month, and the top merchants of the last 30 days. Facts.",
     "input_schema": {**_OBJ, "properties": {
         "period": {"type": "string", "enum": ["last_30d", "trailing_3"]},
         "top_n": {"type": "integer", "minimum": 1, "maximum": 10}}}},
    {"name": "get_recurring", "description": "Recurring money: salary, EMIs, SIPs, rent, subscriptions, bills, with "
     "amounts and next due dates, plus overlapping subscriptions. Facts.",
     "input_schema": {**_OBJ, "properties": {
         "kind": {"type": "string", "enum": ["all", "income", "emi", "sip", "rent", "subscription"]}}}},
    {"name": "forecast", "description": "The 45-day cash forecast for the salary account: chance of dipping below the "
     "safety floor before the next income, bounce risk for upcoming EMIs/SIPs and other debits, balance ranges, and "
     "the forecast's confidence label. Predictions.",
     "input_schema": {**_OBJ, "properties": {}}},
    {"name": "list_recommendations", "description": "The ranked actions for this user, each with its simulated "
     "impact (12-month score, rupees, bounce risk), downside and assumptions; the combined plan; and what-ifs to "
     "explore. Recommendations.",
     "input_schema": {**_OBJ, "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 5}}}},
    {"name": "simulate_action", "description": "Simulate one action or what-if. new_emi needs principal_rupees (an "
     "amount the user gave) and optionally tenure_months and annual_rate_pct (only if the user gave them). "
     "change_emi_tenure takes a loan name. delay_purchase needs amount_rupees. The other types simulate the user's "
     "own recommendation of that type.",
     "input_schema": {**_OBJ, "required": ["action"], "properties": {"action": {
         **_OBJ, "required": ["type"], "properties": {
             "type": {"type": "string", "enum": [*WHAT_IF_TYPES, *[a for a in AUTO_TYPES if a != "link_account"]]},
             "params": {**_OBJ, "properties": {
                 "principal_rupees": {"type": "integer"}, "tenure_months": {"type": "integer"},
                 "annual_rate_pct": {"type": "number"}, "loan": {"type": "string"},
                 "amount_rupees": {"type": "integer"}}}}}}}},
    {"name": "explain_change", "description": "What changed since the previous snapshot: score and band, pillar "
     "and metric changes, reason codes, bounce-risk change and recommendation changes with their causes.",
     "input_schema": {**_OBJ, "properties": {}}},
]

RESPOND_SPEC = {
    "name": "respond",
    "description": "The only way to answer the user. 1 to 8 statements, each one claim with a label and the "
                   "registry ids of every number it contains. The result says whether the answer passed validation; "
                   "if not, fix the listed errors and call respond again.",
    "strict": True,
    "input_schema": {**_OBJ, "required": ["language", "statements"], "properties": {
        "language": {"type": "string", "enum": ["en", "hi", "hinglish"]},
        "statements": {"type": "array", "items": {
            **_OBJ, "required": ["label", "text", "refs"], "properties": {
                "label": {"type": "string", "enum": ["FACT", "PREDICTION", "RECOMMENDATION"]},
                "text": {"type": "string"},
                "refs": {"type": "array", "items": {"type": "string"}}}}}}},
}


def run_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    fn = TOOLS.get(name)
    if fn is None:
        raise ToolError(f"unknown tool {name!r}")
    if not isinstance(args, dict):
        raise ToolError("tool input must be an object")
    return fn(ctx, args)
