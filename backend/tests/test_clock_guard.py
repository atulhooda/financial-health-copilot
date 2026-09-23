"""SPEC D5: time is read only through app/core/clock.py."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
FORBIDDEN = {("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today"),
             ("time", "time"), ("time", "time_ns"), ("time", "localtime"), ("time", "gmtime")}


def _dotted(node) -> list[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def test_no_direct_clock_reads_outside_clock_module():
    offenders = []
    for path in APP.rglob("*.py"):
        if path.relative_to(APP).as_posix() == "core/clock.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if len(name) >= 2 and (name[-2], name[-1]) in FORBIDDEN:
                    offenders.append(f"{path.relative_to(APP)}:{node.lineno} {'.'.join(name)}()")
    assert not offenders, "direct clock reads (use app.core.clock):\n" + "\n".join(offenders)


def test_guard_catches_a_violation(tmp_path):
    bad = "import datetime as dt\nx = dt.datetime.now()\ny = dt.date.today()\n"
    calls = [_dotted(n.func) for n in ast.walk(ast.parse(bad)) if isinstance(n, ast.Call)]
    assert ["dt", "datetime", "now"] in calls and ["dt", "date", "today"] in calls
