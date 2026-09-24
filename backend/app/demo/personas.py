"""Seeded, parameterised personas (SPEC §9). Parameters are generator INPUTS; engine numbers are outputs.

Any tuning of these parameters is recorded in docs/DEMO.md §5.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from app.core.dates import add_months, daterange, roll_back_weekend
from app.core.seeding import rng_for
from app.demo.world import GAccount, World, sms_text
from app.engines.emi import emi_paise

HISTORY_START = dt.date(2026, 3, 21)
T0 = dt.date(2026, 9, 20)
WORLD_END = dt.date(2026, 11, 20)


@dataclass
class Spend:
    """A discretionary spend stream: Poisson count per month, log-normal amounts."""

    per_month: float
    median_rupees: float
    sigma: float
    merchants: list[tuple[str, str]]  # (payee as it appears in narrations, VPA or "")
    account: str = "sal"
    channel: str = "upi"  # upi | card | atm
    weekend_boost: float = 1.0


@dataclass
class PersonaA:
    """Golden demo: salaried, Pune."""

    user_id: str = "demo-a"
    holder: dict = field(default_factory=lambda: {
        "name": "Aarav Deshmukh", "dob": "1994-06-14", "mobile": "9823012345", "email": "aarav.deshmukh@example.in",
        "pan": "ABCPD1234K", "nominee": "Meera Deshmukh"})
    salary_rupees: int = 92_000
    hike_share: float = 0.12
    hike_from: dt.date = dt.date(2026, 10, 1)
    sal_opening: int = 14_000
    sav_opening: int = 38_000
    sweep_to_savings: int = 8_000  # monthly self-transfer on day 2
    rent: int = 24_000
    landlord: str = "RAMESH KULKARNI"
    sip: int = 5_000
    loan1: dict = field(default_factory=lambda: {"principal": 185_000, "rate_pct": 13.0, "tenure": 24,
                                                 "first_due": dt.date(2026, 1, 10)})
    loan2: dict = field(default_factory=lambda: {"principal": 200_000, "rate_pct": 14.0, "tenure": 24,
                                                 "disbursed": dt.date(2026, 11, 20), "first_due": dt.date(2026, 12, 5)})
    card_limit: int = 150_000
    card_opening_carry: int = 12_000
    card_monthly_rate: float = 0.035
    card_pay_ratio: float = 0.30
    card_statement_day: int = 18
    card_due_days: int = 20
    ott: list = field(default_factory=lambda: [("NETFLIX", "netflix@hdfcbank", 649, 12),
                                               ("AMAZON PRIME", "amazonprime@apl", 299, 16),
                                               ("JIOHOTSTAR", "jiohotstar@icici", 299, 22)])
    sms_share: float = 0.15
    # D19 decision (docs/DEMO.md): Android SMS permission is all-or-nothing, so the phone sees the Axis card's
    # spend SMS from day one. The card is therefore VISIBLE at T0 (sms_only); linking at +1 reveals the
    # statement: revolving balance, interest/GST, limit and utilisation, not the purchases.
    card_sms: bool = True
    # Demo beat (DEMO.md): a home-tiffin service the ML can't place -> Uncategorised -> user correction -> rule.
    # Paid monthly on the Axis card (seen via card SMS at T0). Fixed day and amount: draws nothing from the
    # persona's random stream.
    tiffin: tuple = ("GHARGUTI DABBA", 1_800, 3)
    # "Spend what's in the account": on weekends, spend this share of the operating balance above
    # (obligations due in the next 14 days + threshold). Models the behaviour D7's sweep assumption names.
    spend_down_share: float = 0.6
    spend_down_threshold: int = 20_000
    spend_down_merchants: list = field(default_factory=lambda: [
        ("CROMA", "croma@hdfcbank"), ("DECATHLON", "decathlon@icici"), ("MAKEMYTRIP", "makemytrip@icici"),
        ("AMAZON", "amazon@apl"), ("BOOKMYSHOW", "bookmyshow@axisbank"), ("VAISHALI RESTAURANT", "vaishali@paytm")])
    spends: dict = field(default_factory=lambda: {
        "groceries": Spend(11, 480, 0.7, [("BLINKIT", "blinkit@hdfcbank"), ("ZEPTO", "zepto@ybl"),
                                          ("BIGBASKET", "bigbasket@icici"), ("DURGA KIRANA STORES", "durgakirana@ybl")]),
        "food": Spend(6, 420, 0.45, [("SWIGGY", "swiggy@axisbank"), ("ZOMATO", "zomato@hdfcbank")], weekend_boost=1.6),
        "transport": Spend(10, 190, 0.6, [("UBER", "uber@axisbank"), ("RAPIDO", "rapido@ybl"),
                                          ("PUNE METRO", "punemetro@sbi")]),
        "local": Spend(5, 350, 0.8, [("VAISHALI RESTAURANT", "vaishali@paytm"), ("KOTHRUD MEDICAL", "kothrudmed@ybl"),
                                     ("DECCAN PETROLEUM", "deccanpetro@okicici"), ("BANER FASHION", "banerfashion@ybl")]),
        "p2p": Spend(2, 600, 0.6, [("ROHIT JOSHI", "rohit.joshi@okaxis"), ("SNEHA PATIL", "9850012345@ybl")]),
        "card": Spend(14, 1250, 0.9, [("AMAZON PAY INDIA", ""), ("MYNTRA DESIGNS", ""), ("FLIPKART INTERNET", ""),
                                      ("HPCL SHREE AUTO FUELS", ""), ("STARBUCKS COFFEE", ""),
                                      ("SHIVAJI GARMENTS", "")], account="card", channel="card", weekend_boost=1.3),
        "atm": Spend(0.7, 2000, 0.01, [("ATM", "")], channel="atm"),
    })


def _amount(rng: np.random.Generator, median: float, sigma: float) -> int:
    rupees = float(median * np.exp(rng.normal(0.0, sigma)))
    return max(10, int(round(rupees))) * 100


def _loan_schedule(principal: int, rate_pct: float, tenure: int, first_due: dt.date) -> dict:
    emi = emi_paise(principal * 100, round(rate_pct * 100), tenure)
    return {"principal": principal * 100, "emi": emi, "tenure": tenure, "rate_bps": round(rate_pct * 100),
            "due_dates": [add_months(first_due, i) for i in range(tenure)]}


def _outstanding_after(sched: dict, n_paid: int) -> int:
    bal, r = sched["principal"], sched["rate_bps"] / 10000 / 12
    for _ in range(n_paid):
        bal -= sched["emi"] - int(round(bal * r))
    return bal


def salary_paydays(start: dt.date, end: dt.date, day_of_month: int = 1) -> dict[dt.date, dt.date]:
    """payday -> the month it pays for. A weekend payday moves to the preceding Friday."""
    out = {}
    m = dt.date(start.year, start.month, 1)
    while m <= add_months(end, 1, 1):
        pay = roll_back_weekend(dt.date(m.year, m.month, day_of_month))
        if start <= pay <= end:
            out[pay] = dt.date(m.year, m.month, 1)
        m = add_months(m, 1, 1)
    return out


def _cycle_factor(day: dt.date, payday: int = 1) -> float:
    """People spend more right after payday and tighten before the next one."""
    d = (day.day - payday) % 30
    return 1.3 if d < 7 else (0.8 if d > 22 else 1.0)


def build_persona_a(p: PersonaA | None = None) -> World:
    p = p or PersonaA()
    rng = rng_for("persona", p.user_id)
    w = World(p.user_id, rng)
    holder = [p.holder]
    w.add_account(GAccount("sal", "DEPOSIT", "HDFC-FIP", "XXXXXXXX4321", holder, p.sal_opening * 100, sms_bank="hdfc"))
    w.add_account(GAccount("sav", "DEPOSIT", "ICICI-FIP", "XXXXXXXX7788", holder, p.sav_opening * 100))
    s1 = _loan_schedule(**{k: p.loan1[k] for k in ("principal", "rate_pct", "tenure", "first_due")})
    paid_before = sum(1 for d in s1["due_dates"] if d < HISTORY_START)
    w.add_account(GAccount("loan1", "TERM_LOAN", "BAJAJ-FIP", "XXXXXX0931", holder, _outstanding_after(s1, paid_before),
                           acc_type="LOAN", summary={"interestRate": f"{p.loan1['rate_pct']:.2f}",
                                                     "lenderKey": "bajaj_finance", "repaymentFrequency": "MONTHLY"}))
    w.add_account(GAccount("card", "CREDIT_CARD", "AXIS-FIP", "XXXXXXXXXXXX9012", holder, p.card_opening_carry * 100,
                           acc_type="CREDIT_CARD", summary={
                               "creditLimit_paise": p.card_limit * 100,
                               "interestRateMonthly": f"{p.card_monthly_rate * 100:.2f}",
                               "statementDay": p.card_statement_day,
                               "dueDay": (p.card_statement_day + p.card_due_days) % 30}))
    s2 = _loan_schedule(**{k: p.loan2[k] for k in ("principal", "rate_pct", "tenure", "first_due")})
    w.add_account(GAccount("loan2", "TERM_LOAN", "TATACAP-FIP", "XXXXXX5520", holder, 0, acc_type="LOAN",
                           summary={"interestRate": f"{p.loan2['rate_pct']:.2f}", "lenderKey": "tata_capital",
                                    "repaymentFrequency": "MONTHLY"}))
    w.meta["loan_schedules"] = {"loan1": s1, "loan2": s2}
    w.meta["holder_names"] = [p.holder["name"], p.holder["nominee"]]

    # due date -> statement balance; the statement before the history window is already due
    first_stmt = dt.date(HISTORY_START.year, HISTORY_START.month, p.card_statement_day)
    card_due: dict[dt.date, int] = {first_stmt + dt.timedelta(days=p.card_due_days): p.card_opening_carry * 100}
    last_statement_balance = p.card_opening_carry * 100
    carried = p.card_opening_carry * 100
    sal4 = w.accounts["sal"].last4

    def obligations(day: dt.date, horizon: int) -> int:
        """What the persona knows is due from the salary account in the next `horizon` days."""
        total = 0
        for d in daterange(day + dt.timedelta(days=1), day + dt.timedelta(days=horizon)):
            total += (p.rent * 100 if d.day == 5 else 0) + (p.sip * 100 if d.day == 7 else 0)
            total += s1["emi"] if d in s1["due_dates"] else 0
            total += sum(r * 100 for _, _, r, dom in p.ott if dom == d.day)
            total += int(card_due.get(d, 0) * p.card_pay_ratio)
        return total

    def upi_debit(day, acc, payee, vpa, amount, remark="Payment", sms_ok=True):
        ref = w.ref12()
        sms = None
        if acc == "sal" and sms_ok and rng.random() < p.sms_share:
            sms = sms_text("hdfc", "upi_debit", sal4, amount, day, payee, ref)
        return w.debit(acc, day, amount, f"UPI/DR/{ref}/{payee}/HDFC/{vpa}/{remark}", "UPI", ref=ref, sms=sms)

    def mandate(day, acc, entity, amount, umrn, bounce_fee=590_00, sms=True):
        narr = f"NACH-DR-{entity}-{umrn}"
        text = sms_text("hdfc", "mandate", sal4, amount, day, f"NACH-DR-{entity}", None,
                        max(0, w.balance[acc] - amount)) if sms and acc == "sal" else None
        t = w.debit(acc, day, amount, narr, "OTHERS", sms=text)
        if t is None:  # insufficient funds: mandate bounces, bank levies a return charge
            w.debit(acc, day, bounce_fee, f"NACH RTN CHRG-{entity}-{umrn}", "OTHERS", force=True)
        return t

    paydays = salary_paydays(HISTORY_START, WORLD_END)
    for day in daterange(HISTORY_START, WORLD_END):
        # ---- credits first --------------------------------------------------------------
        if day in paydays:
            credit_month = paydays[day]
            amount = p.salary_rupees * 100
            if credit_month >= p.hike_from:
                amount = int(round(p.salary_rupees * (1 + p.hike_share))) * 100
            narr = f"NEFT CR-CITI0000001-ACME TECHNOLOGIES PVT LTD-SALARY {credit_month:%b%y}".upper()
            text = sms_text("hdfc", "credit", sal4, amount, day, narr.replace("NEFT CR", "NEFT Cr"), None,
                            w.balance["sal"] + amount)
            w.credit("sal", day, amount, narr, "FT", sms=text)
        if day.month in (3, 6, 9, 12) and day.day == 30:
            w.credit("sav", day, int(w.balance["sav"] * 0.03 / 4), "CREDIT INTEREST CAPITALISED", "OTHERS")

        # ---- scheduled debits ---------------------------------------------------------------
        if day.day == 2:
            amt = p.sweep_to_savings * 100
            if w.can_debit("sal", amt, keep=5_000_00):
                ref = w.ref12()
                w.debit("sal", day, amt, f"IMPS/P2A/{ref}/SELF/ICICI/XX7788 SAVINGS", "FT", ref=ref)
                w.credit("sav", day, amt, f"IMPS/P2A/{ref}/AARAV DESHMUKH/HDFC/FROM XX4321", "FT", ref=ref)
        if day.day == 5:
            ref = w.ref12()
            narr = f"NEFT-HDFC0{ref[:6]}-{p.landlord}-RENT {day:%b}-{ref}".upper()
            if w.debit("sal", day, p.rent * 100, narr, "FT") is None:
                # rent is paid from savings when the salary account can't cover it
                w.debit("sav", day, p.rent * 100, f"NEFT-ICIC0{ref[:6]}-{p.landlord}-RENT {day:%b}-{ref}".upper(), "FT")
        if day.day == 7:
            mandate(day, "sal", "ICICIPRUMF", p.sip * 100, "HDFC7021807230011111", sms=False)
        for key, sched, entity, umrn in (("loan1", s1, "BAJAJ FINANCE LTD", "HDFC7021807230034567"),):
            if day in sched["due_dates"]:
                if mandate(day, "sal", entity, sched["emi"], umrn) is not None:
                    interest = int(round(w.balance[key] * sched["rate_bps"] / 10000 / 12))
                    w.debit(key, day, interest, "INTEREST APPLIED", "OTHERS", force=True)
                    w.credit(key, day, sched["emi"], "EMI RECEIVED - THANK YOU", "OTHERS")
        for payee, vpa, rupees, dom in p.ott:
            if day.day == dom:
                upi_debit(day, "sal", payee, vpa, rupees * 100, "AutoPay", sms_ok=False)
        if p.tiffin and day.day == p.tiffin[2]:
            amount = p.tiffin[1] * 100
            avl = w.accounts["card"].summary["creditLimit_paise"] - w.balance["card"] - amount
            sms = sms_text("axis", "card_spend", "9012", amount, day, p.tiffin[0], None, avl) if p.card_sms else None
            w.debit("card", day, amount, f"{p.tiffin[0]} PUNE", "CARD", sms=sms)
        if day.day == 15:
            upi_debit(day, "sal", "MSEDCL", "mahadiscom@sbi", _amount(rng, 1850, 0.2), "Electricity")
        if day.day == 20:
            upi_debit(day, "sal", "AIRTEL", "airtelpostpaid@airtel", 599_00, "Postpaid")

        # ---- card: statement, interest, payment ---------------------------------------------------
        if day.day == p.card_statement_day:
            if carried > 0:
                interest = int(round(carried * p.card_monthly_rate))
                w.debit("card", day, interest, "FINANCE CHARGES", "OTHERS", force=True)
                w.debit("card", day, int(round(interest * 0.18)), "IGST ON FINANCE CHARGES", "OTHERS", force=True)
            last_statement_balance = w.balance["card"]
            card_due[day + dt.timedelta(days=p.card_due_days)] = last_statement_balance
        if day in card_due:
            stmt = card_due.pop(day)
            pay = int(round(stmt * p.card_pay_ratio / 100_00)) * 100_00
            pay = min(pay, max(0, w.balance["sal"] - 1_000_00))
            if pay > 0:
                ref = w.ref12()
                w.debit("sal", day, pay, f"BIL/ONL/{ref[:6]}/AXIS BANK CREDIT CARD/XXXXXXXXXXXX9012", "FT", ref=ref)
                w.credit("card", day, pay, "PAYMENT RECEIVED - THANK YOU", "OTHERS")
            carried = max(0, stmt - pay)

        # ---- discretionary ---------------------------------------------------------------
        for name in sorted(p.spends):
            sp = p.spends[name]
            lam = sp.per_month / 30.4 * _cycle_factor(day) * (sp.weekend_boost if day.weekday() >= 5 else 1.0)
            for _ in range(int(rng.poisson(lam))):
                payee, vpa = sp.merchants[int(rng.integers(len(sp.merchants)))]
                amount = _amount(rng, sp.median_rupees, sp.sigma)
                if sp.channel == "card":
                    if w.can_debit("card", amount):
                        avl = w.accounts["card"].summary["creditLimit_paise"] - w.balance["card"] - amount
                        sms = sms_text("axis", "card_spend", "9012", amount, day, payee, None, avl) if p.card_sms else None
                        w.debit("card", day, amount, f"{payee} PUNE", "CARD", sms=sms)
                elif sp.channel == "atm":
                    w.debit("sal", day, amount, f"ATW-{sal4}-HDFC ATM KOTHRUD PUNE", "ATM")
                else:
                    upi_debit(day, sp.account, payee, vpa, amount)

        # ---- spend-down (balance-driven discretionary) -------------------------------------------
        if day.weekday() >= 5:
            due = obligations(day, 14)
            free = w.balance["sal"] - due - p.spend_down_threshold * 100
            if free > 1_000_00:
                payee, vpa = p.spend_down_merchants[int(rng.integers(len(p.spend_down_merchants)))]
                upi_debit(day, "sal", payee, vpa, int(free * p.spend_down_share / 100) * 100)

        # ---- new personal loan (released only at replay step +3): last event of its day -----------
        if day == p.loan2["disbursed"]:
            principal = p.loan2["principal"] * 100
            w.debit("loan2", day, principal, "DISBURSEMENT TO A/C XX4321", "FT", tag="loan2", force=True)
            w.credit("sal", day, principal - 4_720_00, "LOAN DISB/TATA CAPITAL/XX5520", "FT", tag="loan2")
    return w


# =====================================================================================================
@dataclass
class PersonaB:
    """Gig worker, Bengaluru: irregular weekly platform payouts, one account, no card."""

    user_id: str = "demo-b"
    holder: dict = field(default_factory=lambda: {
        "name": "Farhan Shaikh", "dob": "1998-02-03", "mobile": "9741012345", "email": "farhan.s@example.in",
        "pan": "BQRPS4321L", "nominee": "Nasreen Shaikh"})
    opening: int = 9_000
    payout_platforms: list = field(default_factory=lambda: [
        ("BUNDL TECH PAYOUT", "BUNDL TECHNOLOGIES", 0.55), ("RAPIDO CAPTAIN PAYOUT", "ROPPEN TRANSPORTATION", 0.45)])
    weekly_payout_median: int = 5_200
    payout_cv: float = 0.35
    missed_week_prob: float = 0.12
    rent: int = 11_000
    landlord: str = "MANJUNATH GOWDA"
    phone_emi: dict = field(default_factory=lambda: {"principal": 24_000, "rate_pct": 16.0, "tenure": 12,
                                                     "first_due": dt.date(2026, 2, 5)})
    recharge: int = 349
    spends: dict = field(default_factory=lambda: {
        "fuel": Spend(9, 250, 0.3, [("INDIAN OIL", "iocl@ybl"), ("KORAMANGALA FUELS", "kmfuels@okicici")]),
        "food": Spend(12, 140, 0.5, [("INDIRANAGAR SNACKS CENTRE", "indsnacks@paytm"), ("SWIGGY", "swiggy@axisbank")]),
        "groceries": Spend(8, 320, 0.6, [("KORAMANGALA KIRANA STORES", "kmkirana@ybl"), ("ZEPTO", "zepto@ybl")]),
        "p2p": Spend(3, 700, 0.6, [("SALIM KHAN", "9880012345@ybl"), ("AYESHA SHAIKH", "ayesha.s@okaxis")]),
    })


def build_persona_b(p: PersonaB | None = None) -> World:
    p = p or PersonaB()
    rng = rng_for("persona", p.user_id)
    w = World(p.user_id, rng)
    holder = [p.holder]
    w.add_account(GAccount("sal", "DEPOSIT", "KOTAK-FIP", "XXXXXXXX5566", holder, p.opening * 100, sms_bank="kotak"))
    s1 = _loan_schedule(**p.phone_emi)
    w.meta["loan_schedules"] = {}
    w.meta["holder_names"] = [p.holder["name"], p.holder["nominee"]]
    w.meta["cash"] = []
    acc4 = w.accounts["sal"].last4
    for day in daterange(HISTORY_START, T0):
        if day.weekday() == 0 and rng.random() > p.missed_week_prob:  # Monday payouts
            for narr_key, entity, share in p.payout_platforms:
                amt = _amount(rng, p.weekly_payout_median * share, p.payout_cv)
                w.credit("sal", day, amt, f"NEFT CR-YESB0000001-{entity}-{narr_key}".upper(), "FT")
        if day.day == 3:
            ref = w.ref12()
            w.debit("sal", day, p.rent * 100, f"UPI/DR/{ref}/{p.landlord}/KKBK/9900012345@ybl/Rent", "UPI", ref=ref)
        if day in s1["due_dates"]:
            if w.debit("sal", day, s1["emi"], "NACH-DR-BAJAJ FINSERV-KKBK7021807230055555", "OTHERS") is None:
                w.debit("sal", day, 590_00, "NACH RTN CHRG-BAJAJ FINSERV", "OTHERS", force=True)
        if day.day == 12:
            ref = w.ref12()
            w.debit("sal", day, p.recharge * 100, f"UPI/DR/{ref}/RELIANCE JIO/KKBK/jio@sbi/Recharge", "UPI", ref=ref)
        for name in sorted(p.spends):
            sp = p.spends[name]
            for _ in range(int(rng.poisson(sp.per_month / 30.4))):
                payee, vpa = sp.merchants[int(rng.integers(len(sp.merchants)))]
                amount = _amount(rng, sp.median_rupees, sp.sigma)
                ref = w.ref12()
                sms = sms_text("kotak", "upi_debit", acc4, amount, day, vpa or payee, ref) if rng.random() < 0.2 else None
                w.debit("sal", day, amount, f"UPI/DR/{ref}/{payee}/KKBK/{vpa}/Payment", "UPI", ref=ref, sms=sms)
        if day.weekday() == 5 and rng.random() < 0.5:  # cash spends the phone app logs manually
            w.meta["cash"].append({"client_ref": f"cash-{day.isoformat()}", "txn_date": day.isoformat(),
                                   "amount_paise": _amount(rng, 180, 0.4), "direction": "debit",
                                   "merchant": "Chai and snacks", "category": "dining"})
    return w


# =====================================================================================================
@dataclass
class PersonaC:
    """Family, Delhi NCR: two salaries, home + car loan EMIs, quarterly school fees, card paid in full."""

    user_id: str = "demo-c"
    holder: dict = field(default_factory=lambda: {
        "name": "Rakesh Mehra", "dob": "1985-11-21", "mobile": "9811012345", "email": "rakesh.mehra@example.in",
        "pan": "AKLPM5678Q", "nominee": "Sunita Mehra"})
    spouse: dict = field(default_factory=lambda: {
        "name": "Sunita Mehra", "dob": "1987-04-09", "mobile": "9811054321", "email": "sunita.mehra@example.in",
        "pan": "AKLPM8765R", "nominee": "Rakesh Mehra"})
    salary1: int = 140_000
    salary2: int = 75_000
    household_transfer: int = 40_000
    home_loan: dict = field(default_factory=lambda: {"principal": 4_200_000, "rate_pct": 8.6, "tenure": 240,
                                                     "first_due": dt.date(2022, 5, 5)})
    car_loan: dict = field(default_factory=lambda: {"principal": 650_000, "rate_pct": 9.2, "tenure": 60,
                                                    "first_due": dt.date(2024, 8, 10)})
    school_fee: int = 42_000
    maid: int = 8_000
    sip: int = 15_000
    card_limit: int = 300_000
    annual_prime: tuple = (dt.date(2026, 5, 14), 1_499)  # annual plan: detectable from a single charge (D23)
    netflix_monthly: int = 199
    spends: dict = field(default_factory=lambda: {
        "card": Spend(24, 1400, 0.8, [("DMART AVENUE SUPERMARTS", ""), ("BIGBASKET", ""), ("IOCL SAKET", ""),
                                      ("AMAZON PAY INDIA", ""), ("DOMINOS PIZZA", ""), ("LAJPAT TEXTILES", ""),
                                      ("APOLLO PHARMACY", "")], account="card", channel="card", weekend_boost=1.4),
        "local": Spend(10, 300, 0.6, [("SAKET GENERAL STORES", "saketgen@ybl"), ("DWARKA MEDICOS", "dwarkamed@paytm"),
                                      ("DELHI METRO", "dmrc@sbi")]),
    })


def build_persona_c(p: PersonaC | None = None) -> World:
    p = p or PersonaC()
    rng = rng_for("persona", p.user_id)
    w = World(p.user_id, rng)
    w.add_account(GAccount("sal", "DEPOSIT", "SBI-FIP", "XXXXXXX80021", [p.holder], 85_000 * 100))
    w.add_account(GAccount("sal2", "DEPOSIT", "ICICI-FIP", "XXXXXXXX3344", [p.spouse], 60_000 * 100))
    w.add_account(GAccount("card", "CREDIT_CARD", "HDFC-FIP", "XXXXXXXXXXXX7711", [p.holder], 0, acc_type="CREDIT_CARD",
                           summary={"creditLimit_paise": p.card_limit * 100, "interestRateMonthly": "3.60",
                                    "statementDay": 25, "dueDay": 15}))
    sh = _loan_schedule(**p.home_loan)
    sc = _loan_schedule(**p.car_loan)
    for key, sched, fip, masked, lender in (("home", sh, "SBI-FIP", "XXXXXX7001", "sbi_home_loan"),
                                            ("car", sc, "HDFC-FIP", "XXXXXX7002", "hdfc_car_loan")):
        paid = sum(1 for d in sched["due_dates"] if d < HISTORY_START)
        w.add_account(GAccount(key, "TERM_LOAN", fip, masked, [p.holder], _outstanding_after(sched, paid),
                               acc_type="LOAN", summary={"interestRate": f"{sched['rate_bps'] / 100:.2f}",
                                                         "lenderKey": lender, "repaymentFrequency": "MONTHLY"}))
    w.meta["loan_schedules"] = {"home": sh, "car": sc}
    w.meta["holder_names"] = [p.holder["name"], p.spouse["name"]]
    card_due: dict[dt.date, int] = {}
    for day in daterange(HISTORY_START, T0):
        last_working = roll_back_weekend(add_months(day, 1, 1) - dt.timedelta(days=1))
        if day == last_working:
            w.credit("sal", day, p.salary1 * 100, f"NEFT CR-HDFC0000240-GLOBEX INDIA PVT LTD-SALARY {day:%b%y}".upper())
        if day.day == 1:
            w.credit("sal2", day, p.salary2 * 100, f"NEFT CR-UTIB0000100-INITECH SERVICES-SALARY {day:%b%y}".upper())
        if day.day == 2:
            ref = w.ref12()
            w.debit("sal2", day, p.household_transfer * 100, f"IMPS/P2A/{ref}/RAKESH MEHRA/SBI/HOUSEHOLD", "FT", ref=ref)
            w.credit("sal", day, p.household_transfer * 100, f"IMPS/P2A/{ref}/SUNITA MEHRA/ICICI/HOUSEHOLD", "FT", ref=ref)
        for key, sched, entity in (("home", sh, "SBI HOME LOAN"), ("car", sc, "HDFC AUTO LOAN")):
            if day in sched["due_dates"]:
                w.debit("sal", day, sched["emi"], f"NACH-DR-{entity}-SBIN7021807230077777", "OTHERS", force=True)
                w.debit(key, day, int(round(w.balance[key] * sched["rate_bps"] / 10000 / 12)), "INTEREST APPLIED",
                        "OTHERS", force=True)
                w.credit(key, day, sched["emi"], "EMI RECEIVED", "OTHERS")
        if day.day == 10 and day.month in (4, 7, 10, 1):
            w.debit("sal", day, p.school_fee * 100,
                    f"NEFT-HDFC0000123-DELHI PUBLIC SCHOOL-FEES Q{(day.month - 1) // 3 + 1}-{w.ref12()}", "FT")
        if day.day == 3:
            ref = w.ref12()
            w.debit("sal2", day, p.maid * 100, f"UPI/DR/{ref}/KAMLA DEVI/ICIC/9811099999@ybl/Maid salary", "UPI", ref=ref)
        if day.day == 7:
            w.debit("sal", day, p.sip * 100, "NACH-DR-HDFCMF-SBIN7021807230088888", "OTHERS", force=True)
        if day.day == 14:
            ref = w.ref12()
            w.debit("sal", day, _amount(rng, 2600, 0.25), f"UPI/DR/{ref}/BSES RAJDHANI/SBIN/bses@sbi/Electricity",
                    "UPI", ref=ref)
            ref = w.ref12()
            w.debit("sal", day, 999_00, f"UPI/DR/{ref}/AIRTEL/SBIN/airtel@sbi/Broadband", "UPI", ref=ref)
        if day.day == 25:
            card_due[day + dt.timedelta(days=20)] = w.balance["card"]
        if day in card_due:
            pay = card_due.pop(day)
            if pay > 0:
                ref = w.ref12()
                w.debit("sal", day, pay, f"BIL/ONL/{ref[:6]}/HDFC BANK CREDIT CARD/XXXXXXXXXXXX7711", "FT",
                        ref=ref, force=True)
                w.credit("card", day, pay, "PAYMENT RECEIVED - THANK YOU", "OTHERS")
        if day == p.annual_prime[0]:
            w.debit("card", day, p.annual_prime[1] * 100, "AMAZON PRIME MEMBERSHIP NEW DELHI", "CARD")
        if day.day == 9:
            w.debit("card", day, p.netflix_monthly * 100, "NETFLIX.COM MUMBAI", "CARD")
        for name in sorted(p.spends):
            sp = p.spends[name]
            lam = sp.per_month / 30.4 * (sp.weekend_boost if day.weekday() >= 5 else 1.0)
            for _ in range(int(rng.poisson(lam))):
                payee, vpa = sp.merchants[int(rng.integers(len(sp.merchants)))]
                amount = _amount(rng, sp.median_rupees, sp.sigma)
                if sp.channel == "card":
                    w.debit("card", day, amount, f"{payee} NEW DELHI", "CARD")
                else:
                    ref = w.ref12()
                    w.debit("sal2", day, amount, f"UPI/DR/{ref}/{payee}/ICIC/{vpa}/Payment", "UPI", ref=ref)
    return w


def build(persona_id: str) -> World:
    return {"demo-a": build_persona_a, "demo-b": build_persona_b, "demo-c": build_persona_c}[persona_id]()
