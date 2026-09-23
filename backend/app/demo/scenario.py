"""Raw source payloads per persona and per replay step (docs/DEMO.md §2). Each step is a slice of one world."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.demo.personas import HISTORY_START, T0, WORLD_END, build
from app.demo.world import World
from app.ingest.sms import reference_parse


@dataclass
class Step:
    name: str  # t0 | 1 | 2 | 3
    as_of: dt.date
    label: str
    payloads: list[tuple[str, dict]]  # (source, payload) in ingest order


def phone_sms_payload(w: World, frm: dt.date, to: dt.date, exclude_tags=()) -> dict:
    """What the phone sends: raw SMS are parsed ON DEVICE; only structured fields leave it."""
    txns = []
    for sender, text in w.sms_messages(frm, to, exclude_tags):
        parsed = reference_parse(text, sender)
        if parsed is not None:
            txns.append(parsed.model_dump(mode="json"))
    return {"transactions": txns}


def persona_a_steps(w: World | None = None) -> list[Step]:
    w = w or build("demo-a")
    hold = ("loan2",)
    nxt = T0 + dt.timedelta(days=1)
    return [
        Step("t0", T0, "Baseline: salary, savings and loan linked via AA; card not linked",
             [("aa", w.aa_payload(["sal", "sav", "loan1"], HISTORY_START, T0, hold)),
              ("sms", phone_sms_payload(w, HISTORY_START, T0, hold))]),
        Step("1", T0, "Credit card linked via AA",
             [("aa", w.aa_payload(["card"], HISTORY_START, T0, hold))]),
        Step("2", WORLD_END, "Two more months: salary hike seen in 2 credits",
             [("aa", w.aa_payload(["sal", "sav", "loan1", "card"], nxt, WORLD_END, hold)),
              ("sms", phone_sms_payload(w, nxt, WORLD_END, hold))]),
        Step("3", WORLD_END, "New personal loan linked via AA (with EMI schedule) + disbursal credit",
             [("aa", w.aa_payload(["loan2", "sal"], WORLD_END, WORLD_END))]),
    ]


def seed_payloads(persona_id: str) -> list[tuple[str, dict]]:
    """Full T0 history for personas B and C."""
    w = build(persona_id)
    if persona_id == "demo-b":
        return [("aa", w.aa_payload(["sal"], HISTORY_START, T0)),
                ("sms", phone_sms_payload(w, HISTORY_START, T0)),
                ("manual", {"transactions": w.meta["cash"]})]
    if persona_id == "demo-c":
        return [("aa", w.aa_payload(["sal", "card", "home", "car"], HISTORY_START, T0)),
                ("statement", {"bank": "icici_savings", "account_masked": w.accounts["sal2"].masked,
                               "content": w.csv_statement("sal2", "icici_savings", HISTORY_START, T0)})]
    if persona_id == "demo-a":
        return persona_a_steps(w)[0].payloads
    raise KeyError(persona_id)
