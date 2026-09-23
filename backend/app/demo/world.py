"""A simulated financial world: accounts + a chronological ledger, rendered to raw source payloads.

Generation happens day by day with real balance checks (a UPI payment that would overdraw is declined;
a mandate that would overdraw bounces and incurs a return charge), so the ledger is internally consistent.
Rendering slices one world by date and account into AA JSON, bank CSVs and raw SMS text (SPEC §9).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field

import numpy as np

from app.core.config import load_yaml
from app.core.ids import stable_hash
from app.core.money import group_indian

EXPIRY = "2027-09-20T23:59:59+05:30"
PURPOSE = {"code": "102", "text": "Customer spending patterns, budget or other reportings"}


@dataclass
class GAccount:
    key: str
    fi_type: str  # DEPOSIT | CREDIT_CARD | TERM_LOAN
    fip_name: str
    masked: str
    holders: list[dict]
    opening_paise: int = 0  # deposit: balance; card: outstanding; loan: outstanding principal
    acc_type: str = "SAVINGS"
    summary: dict = field(default_factory=dict)  # static summary fields (limit, rate, ...)
    sms_bank: str | None = None  # render SMS for this account in this bank's format

    @property
    def last4(self) -> str:
        return self.masked[-4:]


@dataclass
class GTxn:
    account: str
    date: dt.date
    seq: int
    amount: int
    direction: str
    narration: str
    mode: str
    ref: str | None = None
    sms: tuple[str, str] | None = None  # (sender, raw text) if the phone also got an SMS
    tag: str | None = None
    balance_after: int = 0
    txn_id: str = ""


class World:
    def __init__(self, persona_id: str, rng: np.random.Generator):
        self.persona_id = persona_id
        self.rng = rng
        self.accounts: dict[str, GAccount] = {}
        self.txns: list[GTxn] = []
        self.balance: dict[str, int] = {}
        self._seq: dict[tuple[str, dt.date], int] = {}
        self.meta: dict = {}

    # ----- ledger -----------------------------------------------------------------
    def add_account(self, a: GAccount) -> None:
        self.accounts[a.key] = a
        self.balance[a.key] = a.opening_paise

    def _post(self, acc: str, date: dt.date, amount: int, direction: str, narration: str, mode: str,
              ref: str | None, sms: tuple[str, str] | None, tag: str | None) -> GTxn:
        a = self.accounts[acc]
        seq = self._seq.get((acc, date), 0)
        self._seq[(acc, date)] = seq + 1
        sign = 1 if direction == "credit" else -1
        if a.fi_type in ("CREDIT_CARD", "TERM_LOAN"):
            sign = -sign  # liability: debits increase what's owed
        self.balance[acc] += sign * amount
        t = GTxn(acc, date, seq, amount, direction, narration, mode, ref, sms, tag, self.balance[acc])
        t.txn_id = f"{acc.upper()}{date:%y%m%d}{seq:03d}{stable_hash(self.persona_id, acc, date, seq, n=6).upper()}"
        self.txns.append(t)
        return t

    def credit(self, acc, date, amount, narration, mode="FT", ref=None, sms=None, tag=None) -> GTxn:
        return self._post(acc, date, int(amount), "credit", narration, mode, ref, sms, tag)

    def can_debit(self, acc: str, amount: int, keep: int = 0) -> bool:
        a = self.accounts[acc]
        if a.fi_type == "CREDIT_CARD":
            return self.balance[acc] + amount <= a.summary["creditLimit_paise"]
        return self.balance[acc] - amount >= keep

    def debit(self, acc, date, amount, narration, mode="FT", ref=None, sms=None, tag=None, force=False) -> GTxn | None:
        if not force and not self.can_debit(acc, int(amount)):
            return None
        return self._post(acc, date, int(amount), "debit", narration, mode, ref, sms, tag)

    def ref12(self) -> str:
        return str(int(self.rng.integers(10**11, 10**12)))

    # ----- rendering ----------------------------------------------------------------
    def _slice(self, acc: str, frm: dt.date, to: dt.date, exclude_tags=()) -> list[GTxn]:
        return [t for t in self.txns if t.account == acc and frm <= t.date <= to and t.tag not in exclude_tags]

    def balance_at(self, acc: str, day: dt.date, exclude_tags=()) -> int:
        rows = [t for t in self.txns if t.account == acc and t.date <= day and t.tag not in exclude_tags]
        return rows[-1].balance_after if rows else self.accounts[acc].opening_paise

    def aa_payload(self, account_keys: list[str], frm: dt.date, to: dt.date, exclude_tags=()) -> dict:
        accs = [self.accounts[k] for k in account_keys]
        consent_id = "cns-" + stable_hash(self.persona_id, ",".join(account_keys), frm, to, n=10)
        fi = []
        for a in accs:
            txns = self._slice(a.key, frm, to, exclude_tags)
            summary = self._summary(a, to, exclude_tags)
            fi.append({
                "fiType": a.fi_type,
                "account": {"maskedAccNumber": a.masked, "type": a.acc_type, "fipName": a.fip_name},
                "profile": {"holders": a.holders},
                "summary": summary,
                "transactions": [{
                    "txnId": t.txn_id, "type": t.direction.upper(), "mode": t.mode,
                    "amount": f"{t.amount / 100:.2f}", "currentBalance": f"{t.balance_after / 100:.2f}",
                    "transactionTimestamp": f"{t.date.isoformat()}T{9 + min(t.seq, 12):02d}:{(t.seq * 7) % 60:02d}:00+05:30",
                    "valueDate": t.date.isoformat(), "narration": t.narration, "reference": t.ref or "",
                } for t in txns],
            })
        return {"consent": {"id": consent_id, "purpose": PURPOSE, "fiTypes": sorted({a.fi_type for a in accs}),
                            "accounts": [a.masked for a in accs],
                            "dataRange": {"from": frm.isoformat(), "to": to.isoformat()}, "expiry": EXPIRY},
                "fi": fi}

    def _summary(self, a: GAccount, to: dt.date, exclude_tags) -> dict:
        bal = self.balance_at(a.key, to, exclude_tags)
        s = {k: v for k, v in a.summary.items() if not k.endswith("_paise")}
        if a.fi_type == "DEPOSIT":
            s.update(currentBalance=f"{bal / 100:.2f}", balanceDate=to.isoformat())
        elif a.fi_type == "CREDIT_CARD":
            s.update(currentDue=f"{bal / 100:.2f}", balanceDate=to.isoformat(),
                     creditLimit=f"{a.summary['creditLimit_paise'] / 100:.2f}")
        else:  # TERM_LOAN
            sched = self.meta["loan_schedules"][a.key]
            nxt = next((d for d in sched["due_dates"] if d > to), None)
            paid = sum(1 for d in sched["due_dates"] if d <= to)
            s.update(outstandingBalance=f"{bal / 100:.2f}", principalAmount=f"{sched['principal'] / 100:.2f}",
                     installmentAmount=f"{sched['emi'] / 100:.2f}", tenureMonths=sched["tenure"],
                     remainingInstallments=sched["tenure"] - paid,
                     nextInstallmentDate=nxt.isoformat() if nxt else None)
        return s

    def csv_statement(self, acc: str, bank: str, frm: dt.date, to: dt.date) -> str:
        cfg = load_yaml(f"banks/{bank}")
        cols = cfg["columns"]
        out = io.StringIO()
        fields = [cols[k] for k in ("date", "narration", "reference", "debit", "credit", "balance") if k in cols]
        w = csv.DictWriter(out, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for t in self._slice(acc, frm, to):
            w.writerow({cols["date"]: t.date.strftime(cfg["date_format"]), cols["narration"]: t.narration,
                        cols["reference"]: t.ref or "",
                        cols["debit"]: f"{t.amount / 100:.2f}" if t.direction == "debit" else "",
                        cols["credit"]: f"{t.amount / 100:.2f}" if t.direction == "credit" else "",
                        cols["balance"]: f"{t.balance_after / 100:.2f}"})
        return out.getvalue()

    def sms_messages(self, frm: dt.date, to: dt.date, exclude_tags=()) -> list[tuple[str, str]]:
        return [t.sms for t in self.txns if t.sms and frm <= t.date <= to and t.tag not in exclude_tags]


# ----- SMS text templates (render side of shared/sms_patterns.yaml) ------------------------------
def _amt(paise: int) -> str:
    return f"{paise / 100:,.2f}"


def _avl(paise: int) -> str:
    return f"{group_indian(paise // 100)}.{paise % 100:02d}"


def sms_text(bank: str, kind: str, acct_last4: str, amount: int, date: dt.date, payee: str, ref: str | None,
             avl: int = 0) -> tuple[str, str]:
    if bank == "hdfc" and kind == "upi_debit":
        return ("VM-HDFCBK", f"Sent Rs.{amount / 100:.2f}\nFrom HDFC Bank A/C *{acct_last4}\nTo {payee}\n"
                             f"On {date:%d/%m/%y}\nRef {ref}\nNot You?\nCall 18002586161/SMS BLOCK UPI to 7308080808")
    if bank == "hdfc" and kind == "credit":
        return ("AD-HDFCBK", f"Update! INR {_amt(amount)} deposited in HDFC Bank A/c XX{acct_last4} on "
                             f"{date.strftime('%d-%b-%y').upper()} for {payee}.Avl bal INR {_avl(avl)}. "
                             f"Cheque deposits in A/C are subject to clearing")
    if bank == "hdfc" and kind == "mandate":
        return ("VK-HDFCBK", f"UPDATE: INR {_amt(amount)} debited from HDFC Bank XX{acct_last4} on "
                             f"{date.strftime('%d-%b-%y').upper()}. Info: {payee}. Avl bal:INR {_avl(avl)}")
    if bank == "kotak" and kind == "upi_debit":
        return ("VM-KOTAKB", f"Sent Rs.{amount / 100:.2f} from Kotak Bank AC X{acct_last4} to {payee} on "
                             f"{date:%d-%m-%y}.UPI Ref {ref}. Not you, https://kotak.com/KBANKT/Fraud")
    if bank == "sbi" and kind == "upi_debit":
        return ("BZ-SBIUPI", f"Dear UPI user A/C X{acct_last4} debited by {amount / 100:.1f} on date "
                             f"{date:%d%b%y} trf to {payee} Refno {ref}. If not u? call 1800111109. -SBI")
    raise ValueError(f"no SMS template for {bank}/{kind}")
