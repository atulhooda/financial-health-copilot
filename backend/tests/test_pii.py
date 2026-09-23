from app.core.pii import find_regex_pii, mask_names, mask_regex


def test_regex_classes():
    text = ("Call 9823012345 or +91 98230 12345? mail aarav.deshmukh@example.in, pay rohit.joshi@okaxis, "
            "A/c XXXXXXXX4321, acct 50100123456789, PAN ABCPD1234K, IFSC HDFC0001234, card 4111 1111 1111 1111")
    masked = mask_regex(text)
    for leak in ("9823012345", "aarav.deshmukh", "rohit.joshi@okaxis", "4321", "50100123456789", "ABCPD1234K",
                 "HDFC0001234", "4111"):
        assert leak not in masked, leak
    assert find_regex_pii(masked) == []


def test_money_is_not_masked():
    assert mask_regex("EMI ₹5,415 for 12 months, ₹1,20,000 income, 52.5%") == \
        "EMI ₹5,415 for 12 months, ₹1,20,000 income, 52.5%"


def test_known_names():
    assert mask_names("Paid Ramesh Kulkarni rent", {"RAMESH KULKARNI": "[CONTACT_01]"}) == "Paid [CONTACT_01] rent"
