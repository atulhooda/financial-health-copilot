"""API v1 (docs/API.md): shapes, labels, traceable texts, writes, erasure, the demo replay and the frozen OpenAPI."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from app.api import present
from app.api.deps import Services
from app.api.main import create_app
from app.api.present import Presenter
from app.copilot.llm.none import NoneClient
from app.copilot.masking import Masker
from app.copilot.validator import validate
from app.core.clock import FixedClock
from app.db.models import USER_SCOPED_MODELS, GlobalCounter, SnapshotDiff, Transaction
from app.db.repo import UserRepo
from app.db.session import make_sessionmaker
from app.demo.personas import STEP2
from app.events.bus import InProcessBus
from app.events.snapshots import latest_snapshot
from tests.conftest import migrate

A = {"X-User-Id": "demo-a"}
READS = ("home", "insights", "forecast", "recommendations", "timeline")


@pytest.fixture(scope="module")
def ro(copilot_world, categoriser):
    """Read-only client over the shared world (A replayed to +3; B and C seeded)."""
    return TestClient(create_app(Services(copilot_world.sf, InProcessBus(), categoriser, FixedClock(STEP2),
                                          NoneClient())))


@pytest.fixture(scope="module")
def rw(tmp_path_factory, categoriser):
    """A private world for writes: A replayed to +3."""
    from app.demo.replay import replay

    eng = create_engine(f"sqlite:///{tmp_path_factory.mktemp('api') / 'rw.db'}", future=True)
    migrate(eng)
    sf = make_sessionmaker(eng)
    bus = InProcessBus()
    replay(sf, bus, categoriser)
    return TestClient(create_app(Services(sf, bus, categoriser, FixedClock(STEP2), NoneClient()))), sf


def test_health_and_auth_errors(ro):
    assert ro.get("/v1/health").json()["db"] is True
    r = ro.get("/v1/home")
    assert r.status_code == 400 and r.json()["error"]["code"] == "MISSING_USER"
    r = ro.get("/v1/home", headers={"X-User-Id": "bad id!"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_USER"
    r = ro.get("/v1/home", headers={"X-User-Id": "nobody"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NO_SNAPSHOT"
    r = ro.get("/v1/home?lang=fr", headers=A)
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("lang", ["en", "hi", "hinglish"])
@pytest.mark.parametrize("user", ["demo-a", "demo-b", "demo-c"])
def test_every_read_endpoint_and_every_text_is_traceable(ro, copilot_world, lang, user):
    """Every labelled item's text passes the same validator as the copilot: each number is one of its values."""
    for ep in READS:
        r = ro.get(f"/v1/{ep}?lang={lang}", headers={"X-User-Id": user})
        assert r.status_code == 200, (ep, r.text[:300])
        body = r.json()
        assert body["meta"]["language"] == lang and body["user_id"] == user
        for item in body["facts"] + body["predictions"] + body["recommendations"]:
            assert item["text"] and (item.get("value") is None or item["value"]["display"])
    with copilot_world.sf() as s:
        snap = latest_snapshot(s, user)
        diff = UserRepo(s, user).get(SnapshotDiff, to_snapshot_id=snap.snapshot_id)
    for build in (present.home, present.insights, present.forecast, present.recommendations):
        pr = Presenter(user, snap, diff.payload if diff else None, lang, Masker())
        build(pr)
        for st in pr.statements:
            v = validate({"language": lang, "statements": [st]}, pr.reg, lang)
            assert v.ok, (build.__name__, st, v.errors)


def test_labels_follow_who_proposed_the_action(ro):
    """D10: ranked actions are RECOMMENDATIONs; the tenure-extension offers are conditional PREDICTIONs."""
    body = ro.get("/v1/recommendations", headers=A).json()
    assert {r["type"] for r in body["recommendations"]} <= {"pay_down_card", "redirect_sweep", "auto_sweep",
                                                            "cancel_overlapping_subs", "link_account"}
    offers = [p for p in body["predictions"] if p["what_if"]]
    assert offers and body["data"]["what_if_offers"] == [p["id"] for p in offers]
    for p in offers:
        assert p["text"].startswith("If you") and p["impact"]["lifetime_cost"]["value"] > 0
        assert "approval" in p["text"] and p["assumptions"]  # its assumptions travel with it
    top = body["recommendations"][0]
    assert top["impact"]["score_12m_with_action"]["unit"] == "score" and top["confidence"]["label"] == "Medium"
    assert top["impact"]["annual_impact"]["unit"] == "paise" and isinstance(top["impact"]["annual_impact"]["value"],
                                                                            int)


def test_simulate_a_what_if_and_a_proposed_action(ro):
    r = ro.post("/v1/simulate", headers=A, json={"action": {"type": "new_emi", "params": {
        "principal_paise": 6_000_000, "tenure_months": 12}}})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["kind"] == "what_if" and body["data"]["emi"]["display"] == "₹5,415"
    assert body["data"]["alt_tenure"]["emi"]["display"] == "₹3,743"
    (w,) = [p for p in body["predictions"] if p["what_if"]]
    assert w["text"].startswith("If you borrow ₹60,000 over 12 months") and w["value"]["display"] == "₹5,415"
    assert body["recommendations"] == []
    r = ro.post("/v1/simulate", headers=A, json={"action": {"type": "pay_down_card"}})
    assert r.status_code == 200 and r.json()["data"]["kind"] == "recommendation"
    assert r.json()["recommendations"][0]["type"] == "pay_down_card"
    bad = ro.post("/v1/simulate", headers=A, json={"action": {"type": "new_emi", "params": {"principal_paise": 5}}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "BAD_PARAMS"


def test_ask_groups_labelled_statements_with_their_sources(ro):
    r = ro.post("/v1/ask", headers=A, json={"message": "Kya main ₹60,000 ka phone 12 months ki EMI pe le sakta hoon?"})
    body = r.json()
    assert r.status_code == 200 and body["path"] == "template" and body["language"] == "hinglish"
    assert body["facts"] and body["predictions"] and body["recommendations"]
    refs = {x for k in ("facts", "predictions", "recommendations") for s in body[k] for x in s["refs"]}
    assert refs <= set(body["sources"])
    tier1 = ro.post("/v1/ask", headers=A, json={"message": "I want to end my life"}).json()
    assert tier1["path"] == "guard" and "112" in tier1["message"]


def test_sms_ingest_is_structured_only(ro):
    txn = {"client_ref": "abcd1234efgh", "pattern_id": "hdfc_upi_debit", "sender_id": "HDFCBK", "account_last4": "4321",
           "amount_paise": 25000, "direction": "debit", "merchant_raw": "SWIGGY", "txn_date": "2026-11-01"}
    r = ro.post("/v1/ingest/sms", headers={"X-User-Id": "sms-probe"},
                json={"transactions": [{**txn, "body": "Rs 250 debited from a/c XX4321"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = ro.post("/v1/ingest/sms", headers={"X-User-Id": "sms-probe"}, json={"transactions": [txn], "text": "raw"})
    assert r.status_code == 422


def test_pdf_statements_are_stubbed(ro):
    r = ro.post("/v1/ingest/statement", headers=A, files={"file": ("s.pdf", b"%PDF-1.4", "application/pdf")},
                data={"bank": "hdfc", "account_masked": "XX4321"})
    assert r.status_code == 501 and r.json()["error"]["code"] == "PDF_NOT_SUPPORTED"


def test_writes_recompute_and_erasure_leaves_nothing(rw):
    client, sf = rw
    with sf() as s:
        before = latest_snapshot(s, "demo-a").snapshot_id
        dabba = s.scalars(select(Transaction).where(Transaction.user_id == "demo-a",
                                                    Transaction.merchant_key == "m:gharguti_dabba")).first()
        loan = s.scalars(select(Transaction).where(Transaction.user_id == "demo-a",
                                                   Transaction.category == "loan_disbursal")).first()
    r = client.post("/v1/merchant-rules?wait=true", headers=A, json={"txn_id": dabba.txn_id, "category": "groceries"})
    assert r.status_code == 200 and r.json()["snapshot_id"] not in (None, before)
    with sf() as s:
        cats = set(s.scalars(select(Transaction.category).where(Transaction.user_id == "demo-a",
                                                                Transaction.merchant_key == "m:gharguti_dabba")))
    assert cats == {"groceries"}
    assert client.post("/v1/merchant-rules", headers=A, json={"merchant_key": "x", "category": "nope"}).json()[
        "error"]["code"] == "UNKNOWN_CATEGORY"
    bad = client.post(f"/v1/loan-cash/{dabba.txn_id}", headers=A, json={"use": "reserve"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "NOT_A_LOAN_DISBURSAL"
    r = client.post(f"/v1/loan-cash/{loan.txn_id}?wait=true", headers=A, json={"use": "reserve"})
    assert r.status_code == 200
    home = client.get("/v1/insights", headers=A).json()
    with sf() as s:
        assert latest_snapshot(s, "demo-a").payload["metrics"]["earmarked_loan_paise"] == 0  # D33: kept as reserve
    assert home["user_id"] == "demo-a"

    with sf() as s:
        counter = (s.get(GlobalCounter, "validator_blocks_total") or GlobalCounter(value=0)).value
    r = client.delete("/v1/user/data", headers=A)
    assert r.status_code == 200 and r.json()["erased"]["transactions"] > 0 and r.json()["receipt_id"]
    with sf() as s:
        for model in USER_SCOPED_MODELS:  # SPEC §11: erasure leaves zero rows for the user in every table
            n = s.scalar(select(func.count()).select_from(model).where(model.user_id == "demo-a"))
            assert n == 0, model.__tablename__
        assert (s.get(GlobalCounter, "validator_blocks_total") or GlobalCounter(value=0)).value == counter
    assert client.get("/v1/home", headers=A).status_code == 404


def test_demo_replay_runs_in_order_only_in_demo_mode(tmp_path, categoriser, monkeypatch):
    from app.core.config import get_settings

    eng = create_engine(f"sqlite:///{tmp_path / 'demo.db'}", future=True)
    migrate(eng)
    client = TestClient(create_app(Services(make_sessionmaker(eng), InProcessBus(), categoriser, None, NoneClient())))
    assert client.post("/v1/demo/replay/t0").status_code == 404  # DEMO_MODE off
    monkeypatch.setattr(get_settings(), "demo_mode", True)
    r = client.post("/v1/demo/replay/t0")
    assert r.status_code == 200 and r.json()["diff"] is None
    r = client.post("/v1/demo/replay/2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "OUT_OF_ORDER"
    r = client.post("/v1/demo/replay/1")
    assert r.status_code == 200 and "ACCOUNT_LINKED" in r.json()["diff"]["reason_codes"]


def test_openapi_is_frozen():
    """docs/openapi.json is the contract the app builds on (docs/API.md). Re-freeze with `make openapi` in the
    same commit as a deliberate change."""
    from app.cli import openapi_json
    from app.core.config import REPO_DIR

    committed = (REPO_DIR / "docs" / "openapi.json").read_text(encoding="utf-8")
    assert json.loads(openapi_json()) == json.loads(committed), "API drifted from docs/openapi.json"
    spec = json.loads(committed)
    header = next(p for p in spec["paths"]["/v1/home"]["get"]["parameters"] if p["name"] == "X-User-Id")
    assert "DEV ONLY" in header["description"]
