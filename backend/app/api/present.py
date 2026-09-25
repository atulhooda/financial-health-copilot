"""Snapshot -> API envelope (docs/API.md). The labelled items come from the same registry, tools and templates as
the copilot, so every item's id, display string and text is the one the validator checks: every number in a `text`
is one of the response's values, and a test runs the validator over every envelope.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.copilot.masking import Masker
from app.copilot.registry import Entry, Registry
from app.copilot.render import render_rec
from app.copilot.tools import PROB_SHOWN, ToolContext, run_tool, safe_name
from app.core.config import load_yaml
from app.core.money import format_inr

UNIT = {"inr": "paise", "pct": "pct", "months": "months", "days": "days", "years": "years", "count": "count",
        "score": "score", "date": "date"}
MAIN = {"new_emi": "emi", "change_emi_tenure": "extra_interest", "delay_purchase": "bounce_after",
        "link_account": "linked_after", "cancel_overlapping_subs": "amount"}  # a simulation's headline number


# ---- quantities ------------------------------------------------------------------------------------------------
def q_entry(e: Entry | None, lang: str) -> dict | None:
    """A registry entry as a Quantity: money in integer paise, percents in percent units, dates ISO."""
    if e is None or not e.numeric:
        return None
    if e.unit == "inr":
        value: object = int((e.value * 100).to_integral_value())
    elif e.unit == "date":
        value = e.value.isoformat()
    elif e.unit in ("count", "score", "days"):
        value = int(e.value)
    else:
        value = float(e.value)
    return {"value": value, "unit": UNIT[e.unit], "display": e.display(lang)}


def q_paise(paise: int | None) -> dict | None:
    return None if paise is None else {"value": int(paise), "unit": "paise", "display": format_inr(int(paise))}


def q_prob(p: float | None, lang: str) -> dict | None:
    """A Monte Carlo chance: never shown as 0% or 100% (D42)."""
    if p is None:
        return None
    if p >= 0.995:
        return {"value": round(p * 100, 1), "unit": "pct", "display": PROB_SHOWN["over"][lang]}
    if p < 0.005:
        return {"value": round(p * 100, 1), "unit": "pct", "display": PROB_SHOWN["under"][lang]}
    return {"value": round(p * 100, 1), "unit": "pct", "display": f"{round(p * 100)}%"}


def q_pct(v: float | None, fraction: bool = True) -> dict | None:
    if v is None:
        return None
    x = v * 100 if fraction else v
    shown = f"{round(x)}%" if abs(x) >= 10 or x == int(x) else f"{round(x, 1)}%"
    return {"value": round(x, 2), "unit": "pct", "display": shown}


def q_num(v, unit: str, lang: str) -> dict | None:
    if v is None:
        return None
    if unit == "months":
        e = Entry("", "fact", "months", Decimal(str(round(v, 1))), "")
        return {"value": float(round(v, 1)), "unit": "months", "display": e.display(lang)}
    return {"value": int(v), "unit": unit, "display": str(int(v))}


class Presenter:
    """Builds one response: runs tools into a fresh registry, then turns chosen entries into labelled items."""

    def __init__(self, user_id: str, snapshot, diff: dict | None, lang: str, masker: Masker | None = None,
                 simulator=None):
        self.user_id, self.snap, self.lang = user_id, snapshot, lang
        self.payload = snapshot.payload
        self.reg = Registry()
        self.ctx = ToolContext(user_id, lang, self.payload, diff, self.reg, simulator=simulator)
        self.masker = masker or Masker()
        self.labels = load_yaml("api_labels")
        self.names = load_yaml("copilot_names")
        self.facts: list[dict] = []
        self.predictions: list[dict] = []
        self.recs: list[dict] = []
        self.statements: list[dict] = []  # every item as {label, text, refs}, for the validator

    # ---- helpers ----
    def run(self, tool: str, args: dict | None = None) -> dict:
        return run_tool(self.ctx, tool, args or {})

    def _title(self, section: str, key: str, **names) -> str:
        t = self.labels[section][key][self.lang]
        return t.format(**names) if names else t

    def _conf_label(self, label: str) -> str:
        loc = self.names["confidence_labels"][label][self.lang]
        return {"en": f"{loc} confidence", "hi": f"भरोसा: {loc}", "hinglish": f"confidence: {loc}"}[self.lang]

    def confidence(self, label: str | None = None, reason: str | None = None) -> dict:
        c = self.payload["confidence"]
        return {"label": label or c["label"], "reason": reason or c["reason"],
                "coverage": q_pct(c.get("coverage")), "caps": c.get("caps") or [], "origins": c.get("origins", 0),
                "days": c.get("days", 0), "explain": c.get("explain", "")}

    def _text(self, kind: str, title: str, display: str, conf: str | None = None) -> str:
        return self.labels["text"][kind][self.lang].format(title=title, display=display, confidence=conf)

    # ---- items ----
    def fact(self, key: str, title: str) -> str | None:
        e = self.reg.by_key.get(key)
        if e is None:
            return None
        text = self._text("fact", title, e.display(self.lang))
        self.facts.append({"id": e.id, "key": key, "title": title, "value": q_entry(e, self.lang), "text": text})
        self.statements.append({"label": "FACT", "text": text, "refs": [e.id]})
        return e.id

    def prediction(self, key: str, title: str) -> str | None:
        e = self.reg.by_key.get(key)
        if e is None:
            return None
        label = self.payload["confidence"]["label"]
        text = self._text("prediction", title, e.display(self.lang), self._conf_label(label))
        kind = "PREDICTION" if e.kind == "prediction" else "FACT"
        self.predictions.append({"id": e.id, "key": key, "title": title, "value": q_entry(e, self.lang),
                                 "confidence": self.confidence(), "text": text, "what_if": False})
        self.statements.append({"label": kind, "text": text, "refs": [e.id]})
        return e.id

    def _assumptions(self, prefix: str) -> list[dict]:
        g = self.ctx.texts.get(f"{prefix}.group")
        out = []
        for e in self.reg.entries.values():
            if e.group != g or e.kind not in ("assumption", "fact", "user_input") or not (e.key or "").startswith(
                    f"{prefix}.assume") and not (e.key or "").endswith(".tenure"):
                continue
            src = {"assumption": "config", "fact": "statement", "user_input": "user"}[e.kind]
            out.append({"id": e.id, "key": self.ctx.texts.get(f"{e.key}.key", "tenure"), "text": e.desc,
                        "value": q_entry(e, self.lang), "source": src})
        return out

    def impact(self, imp: dict) -> dict:
        lang = self.lang
        if imp.get("kind") == "data":
            return {"kind": "data", "accounts_linked_before": q_num(imp.get("accounts_linked_before"), "count", lang),
                    "accounts_linked_after": q_num(imp.get("accounts_linked_after"), "count", lang),
                    "accounts_known": q_num(imp.get("accounts_known"), "count", lang),
                    "unlocks": imp.get("unlocks") or []}
        out = {"kind": "simulation", "dip_saturated": imp.get("dip_saturated")}
        for k in ("annual_impact", "monthly_cashflow", "card_interest_saved_12m", "loan_interest_saved_12m",
                  "savings_interest_foregone_12m", "lifetime_cost"):
            out[k] = q_paise(imp.get(f"{k}_paise"))
        for k in ("score_now", "score_12m_baseline", "score_12m_with_action", "score_delta_12m"):
            out[k] = q_num(imp.get(k), "score", lang)
        for k in ("months_to_clear_card", "buffer_months_now_after", "buffer_months_12m_baseline",
                  "buffer_months_12m_with_action"):
            out[k] = q_num(imp.get(k), "months", lang)
        for k in ("bounce_risk_before", "bounce_risk_after", "dip_probability_before", "dip_probability_after"):
            out[k] = q_prob(imp.get(k), lang)
        return out

    def recommendation(self, prefix: str, rec: dict) -> str | None:
        st = render_rec(self.ctx, prefix)
        if st is None:
            return None
        headline = self.reg.by_key.get(f"{prefix}.{MAIN.get(rec['type'], 'annual')}")
        kind = "recommendation" if st["label"] == "RECOMMENDATION" else "prediction"
        main = headline.id if headline is not None and headline.id in st["refs"] else next(
            (r for r in st["refs"] if self.reg.entries[r].kind == kind), st["refs"][0])
        item = {"id": main, "action_key": rec["action_key"], "type": rec["type"], "rank": rec.get("rank"),
                "title": self._title("recommendations", rec["type"]), "text": self.masker.rehydrate(st["text"]),
                "impact": self.impact(rec["impact"]),
                "confidence": self.confidence(rec["confidence"]["label"], rec["confidence"]["reason"]),
                "assumptions": self._assumptions(prefix), "params": _json_safe(rec.get("params")),
                "extra": _json_safe(rec.get("extra"))}
        if st["label"] == "PREDICTION":  # D10: a what-if is a conditional prediction, not a recommendation
            self.predictions.append({"id": main, "key": f"what_if:{rec['action_key']}", "title": item["title"],
                                     "value": q_entry(self.reg.get(main), self.lang), "confidence": item["confidence"],
                                     "text": item["text"], "what_if": True, "impact": item["impact"],
                                     "assumptions": item["assumptions"]})
        else:
            self.recs.append(item)
        self.statements.append(st)
        return main

    def envelope(self, data: dict) -> dict:
        return {"user_id": self.user_id, "snapshot_id": self.snap.snapshot_id, "as_of": self.snap.as_of,
                "facts": self.facts, "predictions": self.predictions, "recommendations": self.recs, "data": data,
                "meta": {"engine_version": self.payload.get("engine_version", ""),
                         "scoring_version": self.payload.get("scoring_version", 0), "language": self.lang,
                         "generated_at": self.snap.created_at.isoformat() if self.snap.created_at else ""}}


def _json_safe(x):
    if isinstance(x, dict):
        return {str(k): _json_safe(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_json_safe(v) for v in x]
    if isinstance(x, dt.date):
        return x.isoformat()
    return x


# ---- endpoints ---------------------------------------------------------------------------------------------------
def home(pr: Presenter) -> dict:
    p, lang = pr.payload, pr.lang
    pr.run("get_metrics")
    pr.run("forecast")
    pr.run("list_recommendations", {"limit": 1})
    pr.fact("score", pr._title("facts", "score"))
    pillar_names = pr.names["pillars"]
    for pl in p["score"]["pillars"]:
        pr.fact(f"pillar.{pl['key']}.points", pr._title("facts", "pillar", pillar=pillar_names[pl["key"]][lang]))
    bounce_ref = None
    b = pr.reg.by_key.get("bounce.m1.probability")
    if b is not None and b.value >= 10:
        bounce_ref = pr.prediction("bounce.m1.probability",
                                   pr._title("predictions", "bounce", name=pr.ctx.texts["bounce.m1.name"]))
    fc = p["forecast"]
    dip_ref = None
    if fc.get("available") and (fc.get("dip_probability") or 0) > 0.20:
        dip_ref = pr.prediction("forecast.dip", pr._title("predictions", "forecast.dip"))
    if p["recommendations"]:
        pr.recommendation("rec.1", p["recommendations"][0])
    alert = None
    if p["alerts"]:
        kind = p["alerts"][0]["kind"]
        alert = {"kind": kind, "ref": bounce_ref if kind == "bounce_risk" else None}
    elif dip_ref:
        alert = {"kind": "dip_risk", "ref": dip_ref}
    cov = p["metrics"]["coverage"]
    pillars = []
    for pl in p["score"]["pillars"]:
        value = None
        if pl["value"] is not None:
            value = q_num(pl["value"], "months", lang) if pl["unit"] == "months" else q_pct(
                pl["value"], fraction=pl["unit"] == "ratio")
        pillars.append({"key": pl["key"], "title": pillar_names[pl["key"]][lang], "weight": pl["weight"],
                        "value": value, "pillar_score": pl["score"], "contribution": pl["contribution"],
                        "status": pl["status"], "note": pl.get("reason")})
    return pr.envelope({
        "score": q_num(p["score"]["total"], "score", lang), "band": p["score"]["band"],
        "score_coverage": q_pct(p["score"]["score_coverage"], fraction=False), "pillars": pillars,
        "top_drag": p["score"].get("top_drag"), "top_alert": alert,
        "linked_accounts": cov["accounts_linked"], "visible_accounts": cov["accounts_visible"],
        "known_accounts": cov["accounts_known"],
        "accounts": [{"account_id": a["account_id"], "kind": a["kind"], "institution": a["institution"].upper(),
                      "masked": f"••{a['last4']}", "status": a["status"], "role": a["role"]} for a in p["accounts"]]})


def insights(pr: Presenter) -> dict:
    lang = pr.lang
    pr.run("get_metrics")
    sp = pr.run("get_spending", {"period": "last_30d", "top_n": 10})
    rc = pr.run("get_recurring", {"kind": "all"})
    pr.fact("spend.total", pr._title("facts", "spend.total"))
    cats = []
    for i, c in enumerate(sp["categories"], 1):
        key = pr.ctx.texts[f"spend.{i}.category"]
        title = pr.names["categories"].get(key, {}).get(lang, key.replace("_", " "))
        pr.fact(f"spend.{i}.amount", pr._title("facts", "spend", category=title))
        if c["drifting"]:
            pr.fact(f"spend.{i}.above_usual", pr._title("facts", "drift", category=title))
        cats.append({"category": key, "title": title, "amount": q_entry(pr.reg.by_key[f"spend.{i}.amount"], lang),
                     "share": q_entry(pr.reg.by_key.get(f"spend.{i}.share"), lang),
                     "usual_month": q_entry(pr.reg.by_key.get(f"spend.{i}.usual"), lang),
                     "vs_usual": q_entry(pr.reg.by_key.get(f"spend.{i}.above_usual"), lang), "drifting": c["drifting"]})
    ratios = {}
    for key in ("savings_rate", "buffer_months", "emi_to_income", "debt_to_income", "credit_utilisation", "revolving"):
        pr.fact(key, pr._title("facts", key))
        ratios[key] = q_entry(pr.reg.by_key.get(key), lang)
    merchants = [{"merchant": pr.masker.rehydrate(m["merchant"]), "amount": q_entry(pr.reg.get(m["amount"]["id"]),
                                                                                     lang)}
                 for m in sp.get("top_merchants", [])]
    return pr.envelope({
        "spending": {"period": "last_30d", "categories": cats, "top_merchants": merchants},
        "recurring": [{"merchant": pr.masker.rehydrate(it["name"]), "kind": it["kind"], "cadence": it["cadence"],
                       "direction": it["direction"], "amount": q_entry(pr.reg.get(it["amount"]["id"]), lang),
                       "next_due": pr.reg.get(it["next_due"]["id"]).value if it.get("next_due") else None}
                      for it in rc["items"]],
        "overlaps": [{"group": o["group"], "members": o["members"],
                      "monthly_total": q_entry(pr.reg.get(o["monthly_total"]["id"]), lang)} for o in rc["overlaps"]],
        "ratios": ratios})


def forecast(pr: Presenter) -> dict:
    p, lang = pr.payload, pr.lang
    fc = p["forecast"]
    if not fc.get("available"):
        return pr.envelope({"available": False})
    pr.run("forecast")
    pr.fact("floor", pr._title("facts", "floor"))
    pr.fact("forecast.horizon", pr._title("facts", "forecast.horizon"))
    if pr.reg.by_key.get("forecast.next_income") and pr.reg.by_key["forecast.next_income"].kind == "fact":
        pr.fact("forecast.next_income", pr._title("facts", "forecast.next_income"))
    pr.prediction("forecast.dip", pr._title("predictions", "forecast.dip"))
    pr.prediction("forecast.dip_date", pr._title("predictions", "forecast.dip_date"))
    pr.prediction("forecast.low", pr._title("predictions", "forecast.low"))
    pr.prediction("forecast.pre.p50", pr._title("predictions", "forecast.pre.p50"))
    for tag in ("m1", "m2", "o1", "o2"):
        if f"bounce.{tag}.probability" in pr.reg.by_key:
            pr.prediction(f"bounce.{tag}.probability",
                          pr._title("predictions", "bounce", name=pr.ctx.texts[f"bounce.{tag}.name"]))
    acct = next((a for a in p["accounts"] if a["account_id"] == fc["account_id"]), None)
    pre = fc.get("pre_income_p10_p50_p90")
    bt = p.get("backtest") or {}
    return pr.envelope({
        "available": True, "horizon_days": fc["horizon_days"], "floor": q_paise(fc["floor_paise"]),
        "account": {"account_id": acct["account_id"], "institution": acct["institution"].upper(),
                    "masked": f"••{acct['last4']}"} if acct else None,
        "opening": q_paise(fc.get("opening_paise")), "next_income_date": fc.get("next_income_date"),
        "income_basis": fc.get("income_basis"),
        "series": [{"date": d, "p10": a, "p50": b, "p90": c}
                   for d, a, b, c in zip(fc["dates"], fc["p10"], fc["p50"], fc["p90"], strict=True)],
        "dip_probability": q_prob(fc.get("dip_probability"), lang), "dip_saturated": fc.get("dip_saturated"),
        "likely_dip_date": fc.get("likely_dip_date"), "projected_low": q_paise(fc.get("projected_low_paise")),
        "pre_income": {"p10": q_paise(pre[0]), "p50": q_paise(pre[1]), "p90": q_paise(pre[2])} if pre else None,
        "bounce_risks": [{"name": pr.masker.rehydrate(safe_name(b["name"], b.get("merchant_key")
                                                                if str(b.get("merchant_key", "")).startswith("p2p:")
                                                                else None)),
                          "kind": b["kind"], "mandate": b["mandate"], "due_date": b["due_date"],
                          "amount": q_paise(b["amount_paise"]), "probability": q_prob(b["probability"], lang)}
                         for b in fc.get("bounce_risks") or []],
        "scheduled": [{"date": f["date"], "name": pr.masker.rehydrate(safe_name(f["name"])), "kind": f["kind"],
                       "direction": f["direction"], "amount": q_paise(f["amount_paise"]), "source": f["source"],
                       "estimated": f["estimated"]} for f in fc.get("scheduled") or []],
        "assumptions": [{"key": a["key"], "text": a["text"], "amount": q_paise(a.get("amount_paise")),
                         "dip_probability_if_kept": q_prob((a.get("alternative") or {}).get("dip_probability_if_kept"),
                                                           lang)} for a in fc.get("assumptions") or []],
        "dip_probability_if_kept": q_prob(fc.get("dip_probability_if_kept"), lang),
        "confidence": pr.confidence(),
        "backtest": {"origins": bt.get("origins"), "days": bt.get("days"),
                     "coverage_p10_p90": q_pct(bt.get("coverage")), "dip_brier": bt.get("brier"),
                     "calibration_gap": q_pct(bt.get("calibration_gap"))}})


def recommendations(pr: Presenter) -> dict:
    p = pr.payload
    pr.run("list_recommendations", {"limit": 5})
    for i, r in enumerate(p["recommendations"][:5], 1):
        pr.recommendation(f"rec.{i}", r)
    offers = [pr.recommendation(f"offer.{i}", o) for i, o in enumerate((p.get("what_if_offers") or [])[:2], 1)]
    plan = p.get("combined_plan")
    return pr.envelope({
        "ranked": [r["action_key"] for r in p["recommendations"]],
        "combined_plan": {"actions": plan["actions"], "impact": pr.impact(plan["impact"]),
                          "confidence": pr.confidence(plan["confidence"]["label"], plan["confidence"]["reason"])}
        if plan else None,
        "what_if_offers": [o for o in offers if o]})


def timeline(pr: Presenter, snapshots: list, diffs: list) -> dict:
    lang = pr.lang
    if pr.ctx.diff:
        pr.run("explain_change")
        pr.fact("change.score_from", pr._title("facts", "score_before"))
        pr.fact("change.score_to", pr._title("facts", "score_now"))
    return pr.envelope({
        "snapshots": [{"snapshot_id": s.snapshot_id, "seq": s.seq, "trigger": s.trigger, "as_of": s.as_of,
                       "created_at": s.created_at.isoformat() if s.created_at else "",
                       "score": s.payload["score"]["total"], "band": s.payload["score"]["band"],
                       "dip_probability": q_prob(s.payload["forecast"].get("dip_probability"), lang),
                       "max_mandate_bounce": q_prob(s.payload["forecast"].get("max_mandate_bounce"), lang),
                       "top_recs": [r["action_key"] for r in s.payload["recommendations"][:3]]} for s in snapshots],
        "diffs": [{"from": d.from_snapshot_id, "to": d.to_snapshot_id, **_json_safe(d.payload)} for d in diffs]})


def simulate(pr: Presenter, action: dict) -> dict:
    """POST /v1/simulate. The app's numbers are the user's own (user_input). A what-if the user asks about is a
    conditional PREDICTION; simulating one of Hisaab's own action types gives that RECOMMENDATION (D10)."""
    t, params = action["type"], dict(action.get("params") or {})
    args: dict = {}
    if t == "new_emi":
        rupees = int(params["principal_paise"]) // 100
        pr.reg.add("user_input", "inr", int(params["principal_paise"]), "amount to borrow")
        args = {"principal_rupees": rupees, "tenure_months": int(params.get("tenure_months") or 12)}
        pr.reg.add("user_input", "months", args["tenure_months"], "tenure you chose")
        if params.get("annual_rate_bps") is not None:
            pct = Decimal(int(params["annual_rate_bps"])) / 100
            pr.reg.add("user_input", "pct", pct, "interest rate you gave")
            args["annual_rate_pct"] = float(pct)
    elif t == "delay_purchase":
        pr.reg.add("user_input", "inr", int(params["amount_paise"]), "purchase amount")
        args = {"amount_rupees": int(params["amount_paise"]) // 100}
    elif t == "change_emi_tenure" and params.get("loan"):
        args = {"loan": str(params["loan"])}
    out = pr.run("simulate_action", {"action": {"type": t, "params": args}})
    if out.get("available") is False:
        return pr.envelope({"kind": "what_if", "unavailable": out.get("reason")})
    prefix = f"sim.{t}"
    rec = _sim_rec(pr, t)
    pr.recommendation(prefix, rec)
    data: dict = {"kind": "recommendation" if pr.ctx.texts.get(f"{prefix}.proposed", True) else "what_if"}
    if t == "new_emi":
        e = rec["extra"]
        data.update({"emi": q_paise(e["emi_paise"]), "total_interest": q_paise(e["total_interest_paise"]),
                     "alt_tenure": {"tenure_months": q_num(e["alt_tenure"]["tenure_months"], "months", pr.lang),
                                    "emi": q_paise(e["alt_tenure"]["emi_paise"])} if e.get("alt_tenure") else None,
                     "emi_to_income_after": q_pct(rec["impact"].get("emi_to_income_after")),
                     "emi_dates": _json_safe(rec["impact"].get("emi_dates"))})
    return pr.envelope(data)


def _sim_rec(pr: Presenter, t: str) -> dict:
    """The evaluated simulation behind a simulate_action call (the tools keep what they described)."""
    return pr.ctx.texts[f"sim.{t}.raw"]
