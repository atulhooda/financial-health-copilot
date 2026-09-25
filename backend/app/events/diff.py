"""Diff consecutive snapshots into reason codes and recommendation changes (SPEC §7, D24, D31, D34).

Pure: two snapshot payloads (+ the D24 attribution) in, a JSON-able diff out. Every change gets >= 1 reason code,
and every recommendation change says what caused it.
"""
from __future__ import annotations

BOUNCE_STEP = 0.10  # pp change in the largest mandate bounce risk that counts as a change
DIP_STEP = 0.10
INCOME_STEP = 0.05

# Why an action appears (its generator's drivers) is carried in the payload. Why one DISAPPEARS is by type:
REMOVAL_DRIVERS = {
    "auto_sweep": ["NEW_EMI_ADDED", "BOUNCE_RISK_UP", "INCOME_DECREASED"],
    "pay_down_card": ["DEBT_CLEARED"],
    "redirect_sweep": ["DEBT_CLEARED"],
    "cancel_overlapping_subs": ["SUBSCRIPTION_CANCELLED"],
    "link_account": ["ACCOUNT_LINKED"],
}


def _emis(p: dict) -> set[str]:
    return {r["merchant_key"] for r in p["recurring"] if r["kind"] == "emi" and r["active"] and r["direction"] == "debit"}


def _subs(p: dict) -> set[str]:
    return {r["merchant_key"] for r in p["recurring"] if r["kind"] == "subscription" and r["active"]}


def _linked(p: dict) -> set[str]:
    return {a["account_id"] for a in p["accounts"] if a["status"] == "linked"}


def diff_payloads(prev: dict, new: dict, attribution: dict, new_data_revealed: bool) -> dict:
    codes: list[str] = []

    def add(code: str) -> None:
        if code not in codes:
            codes.append(code)

    if new_data_revealed:
        add("NEW_DATA_REVEALED")
    if _linked(new) - _linked(prev):
        add("ACCOUNT_LINKED")
    rp, rn = prev["metrics"]["revolving_paise"], new["metrics"]["revolving_paise"]
    if (not rp) and rn:
        add("HIGH_COST_DEBT_FOUND")
    if rp and rn == 0:
        add("DEBT_CLEARED")
    if new.get("debt_growth") and not prev.get("debt_growth"):
        add("DEBT_GROWING")
    ip, inn = prev["metrics"]["income_monthly_paise"], new["metrics"]["income_monthly_paise"]
    if ip and inn >= ip * (1 + INCOME_STEP):
        add("INCOME_INCREASED")
    if ip and inn <= ip * (1 - INCOME_STEP):
        add("INCOME_DECREASED")
    if _emis(new) - _emis(prev):
        add("NEW_EMI_ADDED")
    if _emis(prev) - _emis(new):
        add("EMI_CLOSED")
    if {o["group"] for o in new["overlaps"]} - {o["group"] for o in prev["overlaps"]}:
        add("SUBSCRIPTION_OVERLAP_FOUND")
    if _subs(prev) - _subs(new):
        add("SUBSCRIPTION_CANCELLED")
    if {d["category"] for d in new["drift"]} - {d["category"] for d in prev["drift"]}:
        add("SPENDING_DRIFT_UP")
    fp, fn = prev["forecast"], new["forecast"]
    bp, bn = fp.get("max_mandate_bounce", 0.0), fn.get("max_mandate_bounce", 0.0)
    if bn - bp >= BOUNCE_STEP:
        add("BOUNCE_RISK_UP")
    if bp - bn >= BOUNCE_STEP:
        add("BOUNCE_RISK_DOWN")
    # D34: dip-based codes only when neither side is saturated (a saturated metric carries no information)
    if fp.get("available") and fn.get("available") and not fp.get("dip_saturated") and not fn.get("dip_saturated"):
        if fn["dip_probability"] - fp["dip_probability"] >= DIP_STEP:
            add("DIP_RISK_UP")
        if fp["dip_probability"] - fn["dip_probability"] >= DIP_STEP:
            add("DIP_RISK_DOWN")
    sp, sn = prev["score"], new["score"]
    if sp["band"] != sn["band"]:
        add("SCORE_BAND_CHANGED")
    if prev["confidence"]["label"] != new["confidence"]["label"]:
        add("CONFIDENCE_CHANGED")

    # ---- recommendation changes ----
    before = {r["action_key"]: r for r in prev["recommendations"]}
    after = {r["action_key"]: r for r in new["recommendations"]}
    rec_changes = []
    for key in sorted(set(before) | set(after), key=lambda k: (after.get(k, before.get(k))["rank"] or 99, k)):
        b, a = before.get(key), after.get(key)
        if b and a and b["rank"] == a["rank"]:
            continue
        if a and not b:
            change, drivers = "added", a["drivers"]
        elif b and not a:
            change, drivers = "removed", REMOVAL_DRIVERS.get(b["type"], [])
        else:
            change, drivers = "rank_changed", a["drivers"]
        caused_by = [c for c in drivers if c in codes]
        if not caused_by:  # nothing about this action changed: it moved because others arrived or left
            caused_by = ["OUTRANKED"] if change == "rank_changed" and a["rank"] > b["rank"] else ["DATA_UPDATED"]
        rec_changes.append({"action_key": key, "type": (a or b)["type"], "title": (a or b)["title"], "change": change,
                            "from_rank": b["rank"] if b else None, "to_rank": a["rank"] if a else None,
                            "caused_by": caused_by})
        add({"added": "REC_ADDED", "removed": "REC_REMOVED", "rank_changed": "REC_RANK_CHANGED"}[change])

    pillars_p = {p["key"]: p for p in sp["pillars"]}
    pillar_deltas = [{"key": p["key"], "from": pillars_p[p["key"]]["contribution"], "to": p["contribution"],
                      "delta": p["contribution"] - pillars_p[p["key"]]["contribution"],
                      "behaviour": attribution["pillars"].get(p["key"], {}).get("behaviour", 0),
                      "new_data": attribution["pillars"].get(p["key"], {}).get("new_data", 0)}
                     for p in sn["pillars"] if p["key"] in pillars_p]
    tracked = ("income_monthly_paise", "spend_monthly_paise", "savings_rate", "buffer_months", "emi_to_income",
               "revolving_paise", "credit_utilisation")
    return {
        "from_as_of": prev["as_of"], "to_as_of": new["as_of"], "reason_codes": codes,
        "score": {"from": sp["total"], "to": sn["total"], "delta": sn["total"] - sp["total"], "band_from": sp["band"],
                  "band_to": sn["band"], "behaviour": attribution["score"]["behaviour"],
                  "new_data": attribution["score"]["new_data"]},
        "pillar_deltas": pillar_deltas,
        "metrics": {k: {"from": prev["metrics"][k], "to": new["metrics"][k]} for k in tracked
                    if prev["metrics"][k] != new["metrics"][k]},
        "bounce": {"from": bp, "to": bn},
        "dip": {"from": fp.get("dip_probability"), "to": fn.get("dip_probability"),
                "saturated": bool(fp.get("dip_saturated") or fn.get("dip_saturated"))},
        "revealed_accounts": attribution["revealed_accounts"],
        "rec_changes": rec_changes,
        "new_alerts": [a for a in new["alerts"] if a["kind"] not in {x["kind"] for x in prev["alerts"]}],
    }
