from app.pipeline.categorise.train import train_and_evaluate


def test_categoriser_holdout_report_and_determinism(categoriser):
    m = categoriser.metrics
    assert m["unseen_merchants"]["accuracy"] >= 0.9
    assert 0 <= m["unseen_merchant_types"]["accuracy"] <= 1  # reported honestly, no threshold
    assert "optimistic" in m["caveat"]
    texts = ["KOTHRUD MEDICAL", "DECCAN PETROLEUM", "VAISHALI RESTAURANT", "DURGA KIRANA STORES"]
    labels, _ = categoriser.predict(texts, [30000] * 4, ["upi"] * 4)
    assert labels == ["health", "fuel", "dining", "groceries"]


def test_training_is_deterministic():
    a, ma = train_and_evaluate(per_merchant=1)
    b, mb = train_and_evaluate(per_merchant=1)
    assert ma == mb
