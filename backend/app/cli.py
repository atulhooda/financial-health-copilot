"""hisaab CLI (Typer)."""
from __future__ import annotations

import json

import typer

from app.core.config import REPO_DIR

app = typer.Typer(no_args_is_help=True, help="Hisaab backend CLI")


@app.command("train-categoriser")
def train_categoriser() -> None:
    """Train the ML fallback categoriser, save the artefact and write docs/CATEGORISER.md."""
    from app.pipeline.categorise.train import train_and_evaluate, write_report

    model, metrics = train_and_evaluate()
    model.save()
    write_report(metrics, REPO_DIR / "docs" / "CATEGORISER.md")
    typer.echo(json.dumps({k: metrics[k] for k in ("unseen_merchants", "unseen_merchant_types")}, indent=1))


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
    """Replay persona A: T0 -> +1 -> +2 -> +3 (docs/DEMO.md). Phase 1: ingest + pipeline per step."""
    from sqlalchemy import func, select

    from app.core.clock import FixedClock, set_clock
    from app.db.models import Account, Transaction
    from app.db.repo import UserRepo
    from app.db.session import session_scope
    from app.demo.scenario import persona_a_steps
    from app.ingest.registry import adapter_for
    from app.ingest.service import ingest_batch
    from app.pipeline.categorise.train import get_categoriser

    cat = get_categoriser()
    with session_scope() as s:
        UserRepo(s, "demo-a").erase_all()
    for step in persona_a_steps():
        clock = FixedClock(step.as_of)
        set_clock(clock)
        for source, payload in step.payloads:
            with session_scope() as s:
                ingest_batch(s, "demo-a", adapter_for(source, clock).parse(payload, "demo-a"), cat, clock)
        with session_scope() as s:
            n = s.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == "demo-a"))
            accts = UserRepo(s, "demo-a").select(Account)
            linked = sum(a.link_status == "linked" for a in accts)
        typer.echo(f"[{step.name:>2}] as_of {step.as_of}  {step.label}\n"
                   f"     canonical txns={n}  accounts linked {linked}/{len(accts)}")
    typer.echo("Snapshots, diffs and recommendation changes arrive in Phase 4.")


if __name__ == "__main__":
    app()
