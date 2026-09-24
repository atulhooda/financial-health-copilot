"""hisaab CLI (Typer)."""
from __future__ import annotations

import json

import typer

from app.core.config import REPO_DIR

app = typer.Typer(no_args_is_help=True, help="Hisaab backend CLI")


@app.command("train-categoriser")
def train_categoriser() -> None:
    """Train the ML fallback categoriser, save the artefact and write docs/CATEGORISER.md."""
    from app.core.config import load_yaml
    from app.pipeline.categorise.train import train_and_evaluate, write_report

    model, metrics = train_and_evaluate()
    model.save()
    write_report(metrics, REPO_DIR / "docs" / "CATEGORISER.md", load_yaml("categories")["ml_min_confidence"])
    typer.echo(json.dumps(metrics["unseen_merchant_types"], indent=1))


@app.command()
def seed() -> None:
    """Reset and load personas: A at T0, B and C in full (SPEC §9)."""
    from app.core.clock import FixedClock, set_clock
    from app.db.repo import UserRepo
    from app.db.session import session_scope
    from app.demo.personas import T0
    from app.demo.scenario import seed_payloads
    from app.ingest.registry import adapter_for
    from app.ingest.service import ingest_batch
    from app.pipeline.categorise.train import get_categoriser

    clock = FixedClock(T0)
    set_clock(clock)
    cat = get_categoriser()
    for pid in ("demo-a", "demo-b", "demo-c"):
        with session_scope() as s:
            UserRepo(s, pid).erase_all()
        for source, payload in seed_payloads(pid):
            with session_scope() as s:
                batch = adapter_for(source, clock).parse(payload, pid)
                r = ingest_batch(s, pid, batch, cat, clock)
            typer.echo(f"{pid:7s} {source:9s} raw={r.raw_count:4d} new={r.raw_new:4d} "
                       f"canonical={r.canonical_total:4d} merged={r.duplicates_merged}")


@app.command()
def demo() -> None:
    """Replay persona A: T0 -> +1 -> +2 -> +3 (docs/DEMO.md). Phase 2: facts, score and new-data attribution."""
    from app.core.clock import FixedClock, set_clock
    from app.core.money import format_inr
    from app.db.repo import UserRepo
    from app.db.session import session_scope
    from app.demo.scenario import persona_a_steps
    from app.engines.attribution import attribute_change
    from app.engines.backtest import forecast_with_confidence
    from app.engines.financial import compute_metrics
    from app.engines.score import compute_score
    from app.engines.view import build_view
    from app.ingest.registry import adapter_for
    from app.ingest.service import ingest_batch, next_ingest_seq
    from app.pipeline.categorise.train import get_categoriser

    cat = get_categoriser()
    fmt = lambda p: "n/a" if p is None else format_inr(p)  # noqa: E731
    pct = lambda x: "n/a" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    with session_scope() as s:
        UserRepo(s, "demo-a").erase_all()
    prev = None
    for step in persona_a_steps():
        clock = FixedClock(step.as_of)
        set_clock(clock)
        for source, payload in step.payloads:
            with session_scope() as s:
                ingest_batch(s, "demo-a", adapter_for(source, clock).parse(payload, "demo-a"), cat, clock)
        with session_scope() as s:
            seq = next_ingest_seq(UserRepo(s, "demo-a")) - 1
            v = build_view(s, "demo-a", step.as_of, cat)
            m = compute_metrics(v)
            sc = compute_score(m)
            att = attribute_change(s, "demo-a", cat, prev[0], prev[1], step.as_of, seq) if prev else None
        fc, conf, _ = forecast_with_confidence(v, m.recurring, m.coverage)
        accts = [a for a in v.accounts.values() if a.institution != "cash"]
        typer.echo(f"\n[{step.name:>2}] as_of {step.as_of}  {step.label}")
        typer.echo(f"     accounts linked {sum(a.linked for a in accts)}/{len(accts)} "
                   f"(visible {sum(a.visible for a in accts)})   score {sc.total} {sc.band} "
                   f"(pillar coverage {sc.score_coverage}%)")
        typer.echo(f"     income {fmt(m.income_monthly_paise)}  spend {fmt(m.spend_monthly_paise)}  "
                   f"savings rate {pct(m.savings_rate)}  buffer {m.buffer_months and round(m.buffer_months, 1)} mo  "
                   f"EMI/income {pct(m.emi_to_income)}  revolving {fmt(m.revolving_paise)}")
        typer.echo("     pillars " + "  ".join(f"{p.key}={p.contribution if p.status == 'ok' else '-'}"
                                             for p in sc.pillars))
        if fc.available:
            top = next((b for b in fc.bounce_risks if b.mandate), fc.bounce_risks[0] if fc.bounce_risks else None)
            typer.echo(f"     forecast: dip below {fmt(fc.floor_paise)} before {fc.next_income_date}: "
                       f"{fc.dip_probability:.0%} (likely {fc.likely_dip_date}); confidence {conf.pct}% {conf.label} "
                       f"[P10-P90 coverage {conf.coverage:.0%} over {conf.origins} backtest origins]")
            if top:
                typer.echo(f"     bounce risk: {top.name} {fmt(top.amount_paise)} on {top.due_date}: {top.probability:.0%}")
        if att:
            tag = "NEW_DATA_REVEALED " if att.new_data_revealed else ""
            typer.echo(f"     change {sc.total - att.score['prev']:+d} = behaviour/time {att.score['behaviour']:+d}"
                       f" + new data {att.score['new_data']:+d}  {tag}")
        prev = (step.as_of, seq)
    typer.echo("\nSnapshots, diffs and recommendation changes arrive in Phase 4.")


@app.command("backtest-report")
def backtest_report() -> None:
    """Rolling-origin backtest for every persona and replay step -> docs/FORECAST.md (SPEC §6.4, D27)."""
    import tempfile

    from sqlalchemy import create_engine

    from app.core.clock import FixedClock
    from app.db.session import make_sessionmaker
    from app.demo.personas import T0
    from app.demo.scenario import persona_a_steps, seed_payloads
    from app.engines.backtest import forecast_with_confidence
    from app.engines.financial import compute_metrics
    from app.engines.view import build_view
    from app.ingest.registry import adapter_for
    from app.ingest.service import ingest_batch
    from app.pipeline.categorise.train import get_categoriser

    cat = get_categoriser()
    eng = create_engine(f"sqlite:///{tempfile.mkdtemp()}/bt.db")
    _migrate(eng)
    s = make_sessionmaker(eng)()
    rows = []

    def run(user, label, as_of):
        v = build_view(s, user, as_of, cat)
        m = compute_metrics(v)
        fc, conf, bt = forecast_with_confidence(v, m.recurring, m.coverage)
        rows.append((user, label, as_of, fc, conf, bt))

    for step in persona_a_steps():
        for source, payload in step.payloads:
            ingest_batch(s, "demo-a", adapter_for(source, FixedClock(step.as_of)).parse(payload, "demo-a"), cat,
                         FixedClock(step.as_of))
        s.commit()
        run("demo-a", f"step {step.name}", step.as_of)
    for user in ("demo-b", "demo-c"):
        for source, payload in seed_payloads(user):
            ingest_batch(s, user, adapter_for(source, FixedClock(T0)).parse(payload, user), cat, FixedClock(T0))
        s.commit()
        run(user, "T0", T0)
    _write_forecast_report(rows, REPO_DIR / "docs" / "FORECAST.md")
    for user, label, _as_of, fc, conf, bt in rows:
        typer.echo(f"{user} {label:7s} coverage={bt.coverage:.1%} origins={len(bt.origins)} brier={bt.brier} "
                   f"dip={fc.dip_probability:.1%} confidence={conf.pct}% {conf.label}")


def _migrate(engine) -> None:
    from alembic import command
    from alembic.config import Config

    from app.core.config import BACKEND_DIR

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app/db/migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def _write_forecast_report(rows, path) -> None:
    pct = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
    lines = [
        "# Forecast backtest",
        "",
        "Generated by `hisaab backtest-report` (deterministic). SPEC §6.4, D27. Personas B and C are untuned:",
        "whatever the engine says about them is the honesty check (Phase 3 review, point 3).",
        "",
        "**P10–P90 interval coverage** is the share of actual end-of-day balances that fell inside the forecast's",
        "10th–90th percentile band, over rolling origins every 15 days (horizon up to 45 days). A well-calibrated",
        "band covers about 80%. This is the number behind confidence and the one we quote.",
        "",
        "| Persona | Point | Origins | Days | P10–P90 coverage | Dip Brier | Mean predicted dip | Observed dip rate |"
        " Confidence |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for user, label, as_of, _fc, conf, bt in rows:
        brier = "n/a" if bt.brier is None else f"{bt.brier:.2f}"
        lines.append(f"| {user} | {label} ({as_of}) | {len(bt.origins)} | {bt.days} | **{pct(bt.coverage)}** | {brier} |"
                     f" {pct(bt.mean_predicted_dip)} | {pct(bt.observed_dip_rate)} | {conf.pct}% {conf.label} |")
    lines += [
        "",
        "## Reading this",
        "",
        "- **Persona A** spends more when its balance is high (weekend spend-down after payday). The bootstrap",
        "  samples discretionary days independently of the balance, so after payday it under-predicts spending,",
        "  and late in the cycle it over-predicts it. The coverage shortfall against 80% is the measured cost of that",
        "  (D27). We did not retune the generator to hide it.",
        "- **Persona B** (gig income) has payouts inside the bootstrap. Its dip calibration shows whether we",
        "  over- or under-warn a user whose income is irregular.",
        "- **Persona C** has most of its operating-account flows scheduled (salary, EMIs, fees, card paid in full),",
        "  so its band is narrow. Coverage tests whether the amount spreads on estimated items are honest.",
        "- Confidence = coverage/80% (capped at 1) × history (months/6) × linked/known accounts. A user whose band",
        "  misses often is told so. See SPEC §6.4 for the one-sentence version.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


@app.command("mask-sms")
def mask_sms_cmd(path: str) -> None:
    """Mask real SMS (one per blank-line-separated block) for use as fixtures (SPEC D28). Review the output."""
    from pathlib import Path

    from app.ingest.sms_mask import mask_sms

    for block in [b for b in Path(path).read_text().split("\n\n") if b.strip()]:
        masked, review = mask_sms(block.strip())
        typer.echo(masked)
        if review:
            typer.echo(f"  !! possible names, mask by hand: {review}")
        typer.echo("")


if __name__ == "__main__":
    app()
