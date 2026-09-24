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
    """Replay persona A T0 -> +3 through the event bus and worker; print the timeline (docs/DEMO.md)."""
    from app.core.money import format_inr
    from app.db.session import get_engine, make_sessionmaker
    from app.demo.replay import replay
    from app.events.bus import get_bus
    from app.pipeline.categorise.train import get_categoriser

    try:
        bus = get_bus()
    except Exception as e:  # noqa: BLE001
        raise typer.Exit(_fail(f"event bus unavailable ({e}); run `make up`, or EVENT_BUS=inprocess make demo")) from e
    results = replay(make_sessionmaker(get_engine()), bus, get_categoriser())
    for r in results:
        _print_step(r, format_inr)


def _fail(msg: str) -> int:
    typer.echo(msg, err=True)
    return 1


def _print_step(r, fmt) -> None:
    p = r.snapshot.payload
    sc, fc, conf = p["score"], p["forecast"], p["confidence"]
    accts = p["accounts"]
    typer.echo(f"\n[{r.step.name:>2}] as_of {r.step.as_of}  {r.step.label}")
    typer.echo(f"     snapshot {r.snapshot.snapshot_id} (seq {r.snapshot.seq}, trigger {r.snapshot.trigger}); "
               f"accounts linked {sum(a['status'] == 'linked' for a in accts)}/{len(accts)}")
    line = f"     score {sc['total']} {sc['band']}"
    if r.diff:
        d = r.diff.payload["score"]
        line += f"  ({d['delta']:+d}: behaviour {d['behaviour']:+d}, new data {d['new_data']:+d})"
    typer.echo(line)
    top = next((a for a in p["alerts"] if a["kind"] == "bounce_risk"), None)
    if top:
        typer.echo(f"     bounce risk: {top['name']} {fmt(top['amount_paise'])} on {top['due_date']}: "
                   f"{top['probability']:.0%}")
    if fc["available"]:
        sat = " (saturated: no dip reason codes, D34)" if fc["dip_saturated"] else ""
        typer.echo(f"     dip below {fmt(fc['floor_paise'])} before {fc['next_income_date']}: "
                   f"{fc['dip_probability']:.0%}{sat}; confidence {conf['label']}: {conf['reason']}")
        if fc.get("dip_probability_if_kept") is not None:
            typer.echo(f"     assumes {fmt(fc['earmarked_loan_paise'])} of loan money goes to its purpose; "
                       f"if you keep it, dip risk is {fc['dip_probability_if_kept']:.0%}")
    dg = p.get("debt_growth")
    if dg:
        typer.echo(f"     debt growing: card balance carried grew {fmt(dg['revolving_from_paise'])} -> "
                   f"{fmt(dg['revolving_to_paise'])} over {dg['statements']} statements while income went "
                   f"{fmt(dg['income_then_paise'])} -> {fmt(dg['income_now_paise'])}")
    for rec in p["recommendations"][:3]:
        i = rec["impact"]
        typer.echo(f"     #{rec['rank']} {rec['title']}")
        typer.echo(f"         12-month score {i['score_12m_baseline']} -> {i['score_12m_with_action']} "
                   f"({i['score_delta_12m']:+d}); {fmt(i['annual_impact_paise'])} a year; bounce risk "
                   f"{i['bounce_risk_before']:.0%} -> {i['bounce_risk_after']:.0%}; confidence {rec['confidence']['label']}")
    if r.diff:
        typer.echo(f"     reason codes: {', '.join(r.diff.reason_codes)}")
        for c in r.diff.payload["rec_changes"]:
            where = {"added": f"new at #{c['to_rank']}", "removed": f"removed (was #{c['from_rank']})",
                     "rank_changed": f"#{c['from_rank']} -> #{c['to_rank']}"}[c["change"]]
            typer.echo(f"       {where}: {c['title']}  [caused by {', '.join(c['caused_by'])}]")


@app.command()
def worker() -> None:
    """Consume data.ingested events and write snapshots (the demo runs the same code in-process)."""
    from app.db.session import get_engine, make_sessionmaker
    from app.events.bus import get_bus
    from app.events.worker import run_forever
    from app.pipeline.categorise.train import get_categoriser

    run_forever(get_bus(), make_sessionmaker(get_engine()), get_categoriser())


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
                   f"dip={fc.dip_probability:.1%} confidence={conf.label} ({conf.reason})")


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
        "| Persona | Point | Origins | Days | P10–P90 coverage | Confidence |",
        "|---|---|---|---|---|---|",
    ]
    for user, label, as_of, _fc, conf, bt in rows:
        lines.append(f"| {user} | {label} ({as_of}) | {len(bt.origins)} | {bt.days} | **{pct(bt.coverage)}** |"
                     f" {conf.label}: {conf.reason} |")
    lines += [
        "",
        "## Dip calibration",
        "",
        "For each backtest origin whose outcome is known: the predicted probability of dipping below the floor before",
        "the next income, and whether the balance actually breached it. **Calibration gap** = |mean predicted − observed",
        "breach frequency|; 0 is perfect. Brier is the mean squared error of the individual predictions.",
        "",
        "| Persona | Point | Outcomes | Mean predicted dip | Observed breach frequency | Calibration gap | Brier |",
        "|---|---|---|---|---|---|---|",
    ]
    for user, label, _as_of, _fc, _conf, bt in rows:
        gap = "n/a" if bt.calibration_gap is None else f"{bt.calibration_gap * 100:.1f} pts"
        brier = "n/a" if bt.brier is None else f"{bt.brier:.2f}"
        lines.append(f"| {user} | {label} | {len(bt.dip_pairs)} | {pct(bt.mean_predicted_dip)} | "
                     f"{pct(bt.observed_dip_rate)} | **{gap}** | {brier} |")
    lines += [
        "",
        "## Where the error comes from: income side vs spend side",
        "",
        "Over the same backtest days, the forecast's mean predicted money in and money out of the operating account",
        "vs what actually happened (predicted − actual, ₹ per 30 days). Net < 0 means the forecast was too pessimistic.",
        "",
        "| Persona | Point | Income side | Spend side | Net |",
        "|---|---|---|---|---|",
    ]
    for user, label, _as_of, _fc, _conf, bt in rows:
        e = bt.side_errors()
        rel = lambda x: "" if x is None else f" ({x:+.0%})"  # noqa: E731
        lines.append(f"| {user} | {label} | {e['income_error_per_30d'] / 100:+,.0f}{rel(e['income_error_rel'])} | "
                     f"{e['spend_error_per_30d'] / 100:+,.0f}{rel(e['spend_error_rel'])} | "
                     f"{e['net_error_per_30d'] / 100:+,.0f} |")
    lines += [
        "",
        "## Reading this",
        "",
        "- **Persona A** spends more when its balance is high (weekend spend-down after payday). The bootstrap",
        "  samples discretionary days independently of the balance, so after payday it under-predicts spending,",
        "  and late in the cycle it over-predicts it. The coverage shortfall against 80% is the measured cost of that",
        "  (D27). We did not retune the generator to hide it.",
        "- **Persona B** (gig income) over-warns: it predicts dips at about a third of origins, and none happened.",
        "  The split shows why. **The income side dominates:** predicted payouts run ~38% below actual, against",
        "  ~31% on the spend side, so net cash is under-predicted. B's history opens with a three-week payout drought",
        "  (random in the generated world, left untuned). The bootstrap treats those weeks as typical, and they sit in",
        "  every pool of up to 180 days. Spend is under-predicted too, because B spent little while broke. The fix",
        "  worth testing next is on the income side (e.g. down-weight days before the first payout, or model payouts",
        "  as their own weekly process). The spend model is not the main cause.",
        "- **Persona C** has most of its operating-account flows scheduled (salary, EMIs, fees, card paid in full),",
        "  so its band is narrow. Coverage tests whether the amount spreads on estimated items are honest.",
        "- Confidence label: High at coverage >= 75%, Medium 60-75%, Low below 60%; history under six months or an",
        "  unlinked account can only cap it. The % shown anywhere is the measured coverage itself (SPEC §6.4).",
        "- Phase 3.5 tested a balance-aware spend model against pre-registered criteria. It failed, and was",
        "  reverted: see `docs/PHASE_3_5.md`.",
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
