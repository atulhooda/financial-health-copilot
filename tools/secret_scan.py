#!/usr/bin/env python3
"""Secret scan (Phase 5 review item 10): fail if anything that looks like a credential is tracked, staged, about to be
committed, or in the commits about to be pushed. Stdlib only, so the pre-push hook needs nothing installed.

  python3 tools/secret_scan.py                 # tracked + untracked-but-not-ignored files
  python3 tools/secret_scan.py --range A..B    # also the lines added in those commits (the pre-push hook)

Findings print the file, line and rule, never the matched value.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

RULES = [
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-style key", re.compile(r"\bsk-(?:proj-|or-v1-)?[A-Za-z0-9_\-]{24,}")),
    ("groq key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("key assignment", re.compile(r"(?i)\b[A-Z0-9_]*(?:API_KEY|SECRET|TOKEN|PASSWORD)\s*[:=]\s*['\"]?"
                                  r"(?![<{$])[A-Za-z0-9/+_\-]{20,}")),
]
ALLOW = "secret-scan: allow"  # a line may opt out (e.g. a test fixture of a fake key)
SKIP = ("tools/secret_scan.py",)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def scan_text(name: str, text: str) -> list[str]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if ALLOW in line:
            continue
        hits += [f"{name}:{n}: looks like a {rule}" for rule, rx in RULES if rx.search(line)]
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", action="append", default=[], help="commit range to scan, e.g. origin/main..HEAD")
    args = ap.parse_args()
    root = git("rev-parse", "--show-toplevel").strip()
    files = set(git("-C", root, "ls-files").split("\n")) | set(
        git("-C", root, "ls-files", "--others", "--exclude-standard").split("\n"))
    hits = []
    for f in sorted(x for x in files if x and not x.startswith(SKIP)):
        if re.search(r"(^|/)\.env$", f):
            hits.append(f"{f}: a .env file must never be tracked")
            continue
        try:
            with open(f"{root}/{f}", encoding="utf-8") as fh:
                hits += scan_text(f, fh.read())
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue  # binary, deleted in the working tree, or a submodule
    for rng in args.range:
        added = [ln[1:] for ln in git("-C", root, "log", "-p", "--no-color", rng).splitlines()
                 if ln.startswith("+") and not ln.startswith("+++")]
        hits += scan_text(f"commits {rng}", "\n".join(added))
    if hits:
        print("secret scan FAILED (values not shown):", *hits, sep="\n  ")
        return 1
    print(f"secret scan: clean ({len(files)} files" + (f", {len(args.range)} commit range(s)" if args.range else "")
          + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
