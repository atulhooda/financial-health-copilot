from __future__ import annotations

from app.core.clock import Clock
from app.ingest.aa_mock import AAMockAdapter
from app.ingest.manual import ManualAdapter
from app.ingest.sms import SmsAdapter
from app.ingest.statements import CsvStatementAdapter


def adapter_for(source: str, clock: Clock | None = None):
    if source == "aa":
        return AAMockAdapter(clock)
    if source == "statement":
        return CsvStatementAdapter()
    if source == "sms":
        return SmsAdapter()
    if source == "manual":
        return ManualAdapter()
    raise KeyError(f"unknown source {source!r}")
