"""Loan cash earmark: ONE rule for the buffer (D29) and the forecast (D33), so they never disagree.

A loan disbursal received in the last 90 days is assumed committed to the loan's purpose, as long as it is
still unspent. "Unspent" uses the running minimum of the account balance since the disbursal:

    unspent = min(amount, max(0, lowest balance since the disbursal − balance just before it))

Once the balance has fallen back to its pre-loan level the money is spent, and a later salary never
re-creates it. The user can say they are keeping the money as a reserve (preference
"loan_cash:<txn_id>" = {"use": "reserve"}), which lifts the earmark everywhere.

Known limitation (D29): on day 91 any money still unspent starts counting as buffer.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import polars as pl

from app.engines.view import View

EARMARK_WINDOW_DAYS = 90


@dataclass(frozen=True)
class Earmark:
    txn_id: str
    account_id: str
    date: dt.date
    amount_paise: int
    unspent_paise: int  # still in the account
    kept_as_reserve: bool  # the user lifted the earmark

    @property
    def earmarked_paise(self) -> int:
        return 0 if self.kept_as_reserve else self.unspent_paise


def pref_key(txn_id: str) -> str:
    return f"loan_cash:{txn_id}"


def loan_earmarks(view: View) -> list[Earmark]:
    tx = view.txns
    out = []
    recent = tx.filter((pl.col("category") == "loan_disbursal") & (pl.col("direction") == "credit")
                       & (pl.col("date") > view.as_of - dt.timedelta(days=EARMARK_WINDOW_DAYS))
                       & pl.col("balance_after").is_not_null()).sort(["date", "seq"])
    for r in recent.iter_rows(named=True):
        acc = tx.filter((pl.col("account_id") == r["account_id"]) & pl.col("balance_after").is_not_null())
        later = acc.filter((pl.col("date") > r["date"]) | ((pl.col("date") == r["date"]) & (pl.col("seq") >= r["seq"])))
        before = r["balance_after"] - r["amount"]
        low = int(later["balance_after"].min()) if later.height else r["balance_after"]
        unspent = min(r["amount"], max(0, low - before))
        kept = view.preferences.get(pref_key(r["txn_id"]), {}).get("use") == "reserve"
        out.append(Earmark(r["txn_id"], r["account_id"], r["date"], r["amount"], unspent, kept))
    return out


def earmarked_by_account(view: View) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in loan_earmarks(view):
        out[e.account_id] = out.get(e.account_id, 0) + e.earmarked_paise
    return out
