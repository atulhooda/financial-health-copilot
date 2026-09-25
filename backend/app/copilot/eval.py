"""Copilot eval (Phase 5 items 7 and 9): 30 questions in three languages, run per candidate model, every run
recorded, candidates ranked by the review's order.

Metrics: template fallback rate, first-draft validator block rate, retry success, label-rule violations (first drafts
and delivered answers), p50/p95 latency, cost (tokens x price), the soft English/Hinglish style metric, and guard,
language and intent accuracy. The ranking: fallback rate, then first-draft block rate, then label violations, then
p95 latency, then cost. The deadline suggestion is the chosen candidate's p95, rounded up to a whole second.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path

from app.copilot.llm.base import LLMClient
from app.copilot.orchestrator import Copilot
from app.core.clock import Clock, get_clock
from app.core.config import REPO_DIR, load_yaml

LABEL_RULES = ("LABEL_KIND", "PRED_NO_CONF", "REC_NO_IMPACT", "ASSUMPTION_NOT_CITED", "CONF_AS_PCT",
               "CONDITION_MISSING")
HISTORY = REPO_DIR / "docs" / "copilot_eval_runs.json"
EVAL_DEADLINE_S = 60.0  # generous, so the eval measures real latency; the product deadline is set from its p95


def run_eval(llm: LLMClient, user_id: str, session_factory=None, categoriser=None, clock: Clock | None = None,
             label: str | None = None) -> dict:
    if session_factory is None:
        from app.db.session import get_engine, make_sessionmaker

        session_factory = make_sessionmaker(get_engine())
    if categoriser is None:
        from app.pipeline.categorise.train import get_categoriser

        categoriser = get_categoriser()
    clock = clock or get_clock()
    cp = Copilot(session_factory, categoriser, llm, clock=clock, persist=False, deadline_s=EVAL_DEADLINE_S)
    rows = []
    for q in load_yaml("copilot_eval")["questions"]:
        a = cp.ask(user_id, q["text"])
        rows.append({"id": q["id"], "text": q["text"], "language": q["language"], "expected_guard": q.get("guard"),
                     "expected_intent": q.get("intent"), "detected_language": a.language, "guard": a.guard,
                     "intent": a.intent, "path": a.path, "fallback_reason": a.fallback_reason,
                     "attempts": a.attempts, "final_errors": a.final_errors, "style_notes": a.style_notes,
                     "tool_calls": a.tool_calls, "usage": a.usage, "latency_ms": a.latency_ms,
                     "statements": len(a.statements), "as_of": a.as_of, "answer": a.statements, "message": a.message,
                     "checkin": a.checkin})
    return {"label": label or (llm.provider if llm.provider == "none" else f"{llm.provider}:{llm.model}"),
            "provider": llm.provider, "model": llm.model, "user": user_id, "ran_at": clock.now().isoformat(),
            "deadline_s": EVAL_DEADLINE_S, "as_of": next((r["as_of"] for r in rows if r["as_of"]), None),
            "rows": rows, "metrics": metrics(rows, llm.provider, llm.model)}


def _rate(n: int, d: int) -> dict:
    return {"n": n, "of": d, "rate": round(n / d, 3) if d else None}


def _pctl(xs: list[int], q: float) -> int | None:
    """Nearest-rank percentile."""
    if not xs:
        return None
    xs = sorted(xs)
    return xs[max(0, math.ceil(q / 100 * len(xs)) - 1)]


def _cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    price = (load_yaml("copilot").get("pricing") or {}).get(model)
    if not price:
        return None
    return (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000


def metrics(rows: list[dict], provider: str, model: str = "") -> dict:
    guarded = [r for r in rows if r["expected_guard"] in ("distress", "scope")]
    answered = [r for r in rows if r not in guarded]
    drafted = [r for r in answered if r["attempts"]]
    blocked_first = [r for r in drafted if not r["attempts"][0]["ok"]]
    retried_ok = [r for r in blocked_first if len(r["attempts"]) > 1 and r["attempts"][1]["ok"]]
    first_codes = Counter(e["code"] for r in drafted for e in r["attempts"][0]["errors"])
    all_codes = Counter(e["code"] for r in drafted for a in r["attempts"] for e in a["errors"])
    lat_all = [r["latency_ms"] for r in rows]
    lat_answered = [r["latency_ms"] for r in answered]
    out = {
        "questions": len(rows),
        "guard_correct": _rate(sum(r["guard"] == r["expected_guard"] for r in rows), len(rows)),
        "language_correct": _rate(sum(r["detected_language"] == r["language"] for r in rows), len(rows)),
        "intent_correct": _rate(sum(r["intent"] == r["expected_intent"] for r in answered), len(answered)),
        "delivered_label_rule_violations": sum(1 for r in rows for _ in r["final_errors"]),
        "style_match": _rate(sum(not r.get("style_notes") for r in answered), len(answered)),
        "median_latency_ms": statistics.median(lat_all) if lat_all else None,
        "p50_latency_ms_answered": _pctl(lat_answered, 50),
        "p95_latency_ms_answered": _pctl(lat_answered, 95),
    }
    if provider != "none":
        tin = sum(int(r["usage"].get("input_tokens") or 0) for r in answered)
        tout = sum(int(r["usage"].get("output_tokens") or 0) for r in answered)
        cost = _cost(model, tin, tout)
        out.update({
            "reached_llm": len(answered),
            "template_fallback_rate": _rate(sum(r["path"] == "template" for r in answered), len(answered)),
            "first_draft_block_rate": _rate(len(blocked_first), len(drafted)),
            "retry_success": _rate(len(retried_ok), len(blocked_first)),
            "fallback_reasons": dict(Counter(r["fallback_reason"] for r in answered if r["fallback_reason"])),
            "first_draft_label_rule_violations": sum(first_codes[c] for c in LABEL_RULES),
            "first_draft_error_codes": dict(first_codes),
            "all_attempt_error_codes": dict(all_codes),
            "median_tool_calls": statistics.median([r["tool_calls"] for r in answered]) if answered else None,
            "tokens_per_question": {"input": round(tin / len(answered)), "output": round(tout / len(answered))}
            if answered else None,
            "cost_usd_per_question": round(cost / len(answered), 5) if cost is not None and answered else None,
        })
    return out


# ---- recording and ranking --------------------------------------------------------------------------------------
def record(result: dict, path: Path = HISTORY) -> list[dict]:
    runs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    runs.append(result)
    path.write_text(json.dumps(runs, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return runs


def _rank_key(m: dict) -> tuple:
    def rate(k):
        v = (m.get(k) or {}).get("rate")
        return 1.0 if v is None else v
    return (rate("template_fallback_rate"), rate("first_draft_block_rate"),
            m.get("first_draft_label_rule_violations", 10**6), m.get("p95_latency_ms_answered") or 10**9,
            m.get("cost_usd_per_question") if m.get("cost_usd_per_question") is not None else float("inf"))


def ranked(runs: list[dict]) -> list[dict]:
    """The latest run of each LLM candidate, best first (review item 9's order)."""
    latest: dict[str, dict] = {}
    for r in runs:
        if r["provider"] != "none":
            latest[r["label"]] = r
    return sorted(latest.values(), key=lambda r: _rank_key(r["metrics"]))


def deadline_from(run: dict) -> int | None:
    p95 = run["metrics"].get("p95_latency_ms_answered")
    return math.ceil(p95 / 1000) if p95 else None


# ---- the report ------------------------------------------------------------------------------------------------
def _fmt_rate(m: dict | None) -> str:
    if not m:
        return "n/a"
    if m["rate"] is None:
        return f"n/a (0 of {m['of']})"
    return f"{m['rate'] * 100:.0f}% ({m['n']}/{m['of']})"


def _ms(v) -> str:
    return "n/a" if v is None else f"{v / 1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


def _usd(v) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def write_report(runs: list[dict], path: Path) -> None:
    order = ranked(runs)
    lines = [
        "# Copilot eval",
        "",
        "Generated by `hisaab eval-copilot` (Phase 5 items 7 and 9). 30 questions: 10 English, 10 Hindi "
        "(Devanagari), 10 Hinglish (Latin script), covering afford-an-EMI, where the money goes, running short, why "
        "the score changed, trade-off questions, the summary, both distress tiers and the scope guard "
        "(`backend/config/copilot_eval.yaml`). Every run is recorded in `docs/copilot_eval_runs.json`.",
        "",
        "Candidates are ranked by: template fallback rate, then first-draft block rate, then label-rule violations, "
        f"then p95 latency, then cost. The eval allows {EVAL_DEADLINE_S:.0f} s per question so it measures real "
        "latency; the product deadline (`COPILOT_DEADLINE_S`) is then set from the chosen candidate's p95.",
        "",
        "## Candidates (latest run of each, best first)",
        "",
    ]
    if order:
        lines += ["| # | candidate | fallback | first-draft blocks | retry fixed | label violations (first drafts) "
                  "| p50 | p95 | cost / question | style match | ran |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for i, r in enumerate(order, 1):
            m = r["metrics"]
            lines.append(f"| {i} | {r['label']} (`{r['model']}`) | {_fmt_rate(m['template_fallback_rate'])} | "
                         f"{_fmt_rate(m['first_draft_block_rate'])} | {_fmt_rate(m['retry_success'])} | "
                         f"{m['first_draft_label_rule_violations']} | {_ms(m['p50_latency_ms_answered'])} | "
                         f"{_ms(m['p95_latency_ms_answered'])} | {_usd(m['cost_usd_per_question'])} | "
                         f"{_fmt_rate(m['style_match'])} | {r['ran_at'][:16]} |")
        best = order[0]
        lines += ["", f"**Pick: {best['label']}** (`{best['model']}`). Suggested `COPILOT_DEADLINE_S`: "
                      f"{deadline_from(best)} (its p95, rounded up)."]
    else:
        lines += ["No LLM candidate has been run yet: no provider key was available. Add keys to the gitignored "
                  "`backend/.env` (names in `backend/.env.example`), then run `uv run hisaab eval-copilot "
                  "--candidates`. Only the template path below has been measured."]
    lines += ["", "## All runs", "", "| ran | candidate | fallback | first-draft blocks | delivered violations | "
              "p95 | guards | language | intents |", "|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        m = r["metrics"]
        lines.append(f"| {r['ran_at'][:16]} | {r['label']} | {_fmt_rate(m.get('template_fallback_rate'))} | "
                     f"{_fmt_rate(m.get('first_draft_block_rate'))} | {m['delivered_label_rule_violations']} | "
                     f"{_ms(m['p95_latency_ms_answered'])} | {_fmt_rate(m['guard_correct'])} | "
                     f"{_fmt_rate(m['language_correct'])} | {_fmt_rate(m['intent_correct'])} |")
    for r in reversed(runs):
        lines += _run_section(r)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _run_section(r: dict) -> list[str]:
    m, rows = r["metrics"], r["rows"]
    llm = r["provider"] != "none"
    out = ["", f"## Run: {r['label']}, {r['ran_at'][:16]}", "",
           f"Provider **{r['provider']}**" + (f", model `{r['model']}`" if llm else " (templates only)") +
           f"; user `{r['user']}`, data as of {r['as_of']}.", "", "| Metric | Value |", "|---|---|"]
    if llm:
        t = m["tokens_per_question"]
        tokens = f"{t['input']} / {t['output']}" if t else "n/a"
        out += [f"| Template fallback rate | {_fmt_rate(m['template_fallback_rate'])} |",
                f"| First-draft validator block rate | {_fmt_rate(m['first_draft_block_rate'])} |",
                f"| Retry success (blocked first drafts fixed on retry) | {_fmt_rate(m['retry_success'])} |",
                f"| Label-rule violations in first drafts ({', '.join(LABEL_RULES)}) | "
                f"{m['first_draft_label_rule_violations']} |",
                f"| Tokens per question (in / out) | {tokens} |",
                f"| Cost per question | {_usd(m['cost_usd_per_question'])} |",
                f"| Median tool calls | {m['median_tool_calls']} |"]
    else:
        out += ["| Fallback, block and retry rates | n/a: no LLM (every answer is a template) |"]
    out += [f"| Label-rule violations in delivered answers (re-validated) | {m['delivered_label_rule_violations']} |",
            f"| Latency p50 / p95, answered | {_ms(m['p50_latency_ms_answered'])} / "
            f"{_ms(m['p95_latency_ms_answered'])} |",
            f"| Style match (soft: English vs Hinglish) | {_fmt_rate(m['style_match'])} |",
            f"| Guard decisions correct | {_fmt_rate(m['guard_correct'])} |",
            f"| Language detected correctly | {_fmt_rate(m['language_correct'])} |",
            f"| Template intent correct | {_fmt_rate(m['intent_correct'])} |"]
    if llm:
        out += ["", f"First-draft error codes: `{m['first_draft_error_codes']}`. All attempts: "
                    f"`{m['all_attempt_error_codes']}`. Fallback reasons: `{m['fallback_reasons']}`."]
    out += ["", "<details><summary>Questions and answers</summary>", "",
            "| id | question | guard | intent | path | first-draft errors | statements | latency |",
            "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        first = ", ".join(sorted({e["code"] for e in row["attempts"][0]["errors"]})) if row["attempts"] else ""
        guard = row["guard"] or "-"
        if row["guard"] != row["expected_guard"]:
            guard += f" (expected {row['expected_guard'] or '-'})"
        intent = row["intent"] or "-"
        if row["expected_intent"] and row["intent"] != row["expected_intent"]:
            intent += f" (expected {row['expected_intent']})"
        path_ = row["path"] + (f" ({row['fallback_reason']})" if row["fallback_reason"] else "")
        text = row["text"].replace("|", "\\|")
        out.append(f"| {row['id']} | {text} | {guard} | {intent} | {path_} | {first or '-'} | {row['statements']} "
                   f"| {_ms(row['latency_ms'])} |")
    out.append("")
    for row in rows:
        out += [f"**{row['id']}** {row['text']}", ""]
        if row["checkin"]:
            out.append(f"- _check-in:_ {row['checkin']}")
        if row["message"]:
            out.append(f"- _{row['path']}:_ {row['message']}")
        out += [f"- **{st['label']}** {st['text']} `{' '.join(st['refs'])}`" for st in row["answer"]]
        out.append("")
    out.append("</details>")
    return out
