"""SPEC D12/D13: one pattern file for Python and Dart; raw SMS never reaches the API."""
import datetime as dt
import re

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import SHARED_DIR
from app.ingest.sms import SmsAdapter, StructuredSmsTxn, load_patterns, reference_parse

# Constructs that differ between Python `re` and Dart `RegExp`, or that we banned (D13).
NON_PORTABLE = [r"\(\?P<", r"\(\?<[A-Za-z]", r"\(\?<[=!]", r"\(\?[aiLmsux]", r"\(\?>", r"\\A", r"\\Z", r"\\z",
                r"[*+?}]\+", r"\(\?#"]


def test_patterns_use_the_shared_regex_subset():
    for p in load_patterns()["patterns"]:
        for bad in NON_PORTABLE:
            assert not re.search(bad, p["regex"]), f"{p['id']}: non-portable construct {bad}"
        compiled = re.compile(p["regex"])
        assert compiled.groupindex == {}, "named groups are not portable"
        assert sorted(p["fields"].values()) == list(range(1, compiled.groups + 1)), f"{p['id']}: fields != groups"
        assert {"amount", "account_last4", "merchant", "date"} <= set(p["fields"])
        assert re.fullmatch(r"[-/ .]*", re.sub(r"YYYY|MON|DD|MM|YY", "", p["date_format"])), p["date_format"]


FIXTURES = yaml.safe_load((SHARED_DIR / "sms_fixtures" / "fixtures.yaml").read_text())


@pytest.mark.parametrize("fx", FIXTURES["positive"], ids=lambda f: f["expect"]["pattern_id"])
def test_positive_fixtures(fx):
    got = reference_parse(fx["sms"], fx["sender"])
    assert got is not None
    exp = dict(fx["expect"])
    exp["txn_date"] = exp["txn_date"] if isinstance(exp["txn_date"], dt.date) else dt.date.fromisoformat(exp["txn_date"])
    for k, v in exp.items():
        assert getattr(got, k) == v, k


@pytest.mark.parametrize("fx", FIXTURES["negative"])
def test_negative_fixtures(fx):
    assert reference_parse(fx["sms"], fx["sender"]) is None


def test_every_pattern_has_a_fixture():
    covered = {f["expect"]["pattern_id"] for f in FIXTURES["positive"]}
    assert covered == {p["id"] for p in load_patterns()["patterns"]}


def test_structured_sms_has_no_room_for_a_body():
    good = reference_parse(FIXTURES["positive"][0]["sms"], FIXTURES["positive"][0]["sender"]).model_dump(mode="json")
    for field in ("body", "text", "sms", "raw"):
        with pytest.raises(ValidationError):
            StructuredSmsTxn.model_validate({**good, field: FIXTURES["positive"][0]["sms"]})
    assert "body" not in StructuredSmsTxn.model_fields
    batch = SmsAdapter().parse({"transactions": [good]}, "u1")
    assert batch.transactions[0].narration == "SMS/SWIGGY"
