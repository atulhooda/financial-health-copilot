"""Build engine Views directly from rows, for unit tests that don't need the DB."""
from __future__ import annotations

import datetime as dt

import polars as pl

from app.engines.view import TXN_SCHEMA, AccountView, View, category_flags


def make_view(rows: list[dict], as_of: dt.date, accounts: dict[str, AccountView] | None = None,
              user_id: str = "u1", floor: int = 500000) -> View:
    flags = category_flags()
    recs = []
    for i, r in enumerate(rows):
        cat = r.get("category", "other")
        recs.append({"txn_id": f"t{i}", "account_id": r.get("account_id", "acc_sal"), "date": r["date"], "seq": i,
                     "amount": r["amount"], "direction": r.get("direction", "debit"), "category": cat,
                     "category_source": "rule", "confidence": 1.0, "merchant_key": r["merchant_key"],
                     "merchant_name": r.get("merchant_name", r["merchant_key"]),
                     "counterparty_type": r.get("counterparty_type", "merchant"), "channel": r.get("channel", "upi"),
                     "is_bounce": False, "balance_after": r.get("balance_after"),
                     "account_kind": r.get("account_kind", "savings"), **flags[cat]})
    txns = pl.DataFrame(recs, schema=TXN_SCHEMA).sort(["date", "account_id", "seq"])
    accounts = accounts or {"acc_sal": AccountView("acc_sal", "savings", "hdfc", "4321", True, rows[0]["date"],
                                                   role="operating")}
    return View(user_id, as_of, accounts, txns, floor, 1)
