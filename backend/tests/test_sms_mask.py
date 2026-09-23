from app.ingest.sms import reference_parse
from app.ingest.sms_mask import mask_sms


def test_masked_sms_still_parses_and_leaks_nothing():
    raw = ("Sent Rs.450.00\nFrom HDFC Bank A/C *4321\nTo RAHUL SHARMA\nOn 12/09/26\nRef 412345678901\n"
           "Not You?\nCall 18002586161/SMS BLOCK UPI to 7308080808")
    masked, review = mask_sms(raw)
    assert "4321" not in masked and "412345678901" not in masked and "7308080808" not in masked
    assert review == ["RAHUL SHARMA"]
    parsed = reference_parse(masked, "VM-HDFCBK")
    assert parsed and parsed.account_last4 == "0000" and parsed.amount_paise == 45000


def test_balances_and_handles_masked():
    masked, _ = mask_sms("Update! INR 92,000.00 deposited in HDFC Bank A/c XX4321 on 01-SEP-26 for UPI from "
                         "ramesh.k@okaxis.Avl bal INR 1,05,234.00.")
    assert "1,05,234" not in masked and "ramesh.k@okaxis" not in masked and "XX0000" in masked
