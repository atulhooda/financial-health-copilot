from app.pipeline.categorise.train import train_and_evaluate


def test_categoriser_holdout_report_and_determinism(categoriser):
    m = categoriser.metrics
    assert m["unseen_merchants"]["accuracy"] >= 0.9
    assert 0 <= m["unseen_merchant_types"]["accuracy"] <= 1  # reported honestly, no threshold
    assert "optimistic" in m["caveat"]
    texts = ["KOTHRUD MEDICAL", "DECCAN PETROLEUM", "VAISHALI RESTAURANT", "DURGA KIRANA STORES"]
    labels, _ = categoriser.predict(texts, [30000] * 4, ["upi"] * 4)
    assert labels == ["health", "fuel", "dining", "groceries"]


def test_report_quotes_only_the_unseen_type_holdout(tmp_path, categoriser):
    from app.core.config import load_yaml
    from app.pipeline.categorise.train import write_report

    out = tmp_path / "r.md"
    write_report(categoriser.metrics, out, load_yaml("categories")["ml_min_confidence"])
    text = out.read_text()
    easy = f"{categoriser.metrics['unseen_merchants']['accuracy']:.0%}"
    assert easy not in text and "(configured)" in text


def test_training_is_deterministic():
    a, ma = train_and_evaluate(per_merchant=1)
    b, mb = train_and_evaluate(per_merchant=1)
    assert ma == mb
