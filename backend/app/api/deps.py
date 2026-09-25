"""Shared services, the dev-only user header, and the error shape (docs/API.md §1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.orm import Session, sessionmaker

from app.api.schemas import Lang
from app.core.clock import Clock, get_clock

USER_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class ApiError(Exception):
    """Rendered as {"error": {"code", "message", "details"}} with the given status."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or {}


@dataclass
class Services:
    """Everything a request needs, created lazily so importing the app (e.g. to export OpenAPI) touches nothing.
    Tests pass their own SQLite session factory, in-process bus, fixed clock and LLM."""

    session_factory: sessionmaker[Session] | None = None
    bus: object | None = None
    categoriser: object | None = None
    clock: Clock | None = None
    llm: object | None = None
    _cache: dict = field(default_factory=dict)

    def sf(self) -> sessionmaker[Session]:
        if self.session_factory is None:
            from app.db.session import get_engine, make_sessionmaker

            self.session_factory = make_sessionmaker(get_engine())
        return self.session_factory

    def get_bus(self):
        if self.bus is None:
            from app.events.bus import get_bus

            self.bus = get_bus()
        return self.bus

    def get_categoriser(self):
        if self.categoriser is None:
            from app.pipeline.categorise.train import get_categoriser

            self.categoriser = get_categoriser()
        return self.categoriser

    def get_clock(self) -> Clock:
        return self.clock or get_clock()

    def get_llm(self) -> tuple[object, str | None]:
        """The configured LLM, or templates with the reason if it can't be built (LLM_PROVIDER=none always works)."""
        if self.llm is not None:
            return self.llm, None
        from app.copilot.llm import LLMUnavailable, get_llm
        from app.copilot.llm.none import NoneClient

        try:
            return get_llm(), None
        except LLMUnavailable as e:
            return NoneClient(), f"llm_unavailable: {e}"


def services(request: Request) -> Services:
    return request.app.state.services


def current_user(x_user_id: Annotated[str | None, Header(
        alias="X-User-Id", description="DEV ONLY: prototype auth. The user whose data this is, e.g. demo-a. "
                                       "Production replaces it with a signed session token.")] = None) -> str:
    if not x_user_id:
        raise ApiError(400, "MISSING_USER", "send the X-User-Id header (dev-only auth)")
    if not USER_RE.match(x_user_id):
        raise ApiError(400, "BAD_USER", "X-User-Id must be 1-64 letters, digits, '_' or '-'")
    return x_user_id


def language(lang: Annotated[Lang, Query(description="language of titles and texts; numbers are digits in all")]
             = "en") -> str:
    return lang


Svc = Annotated[Services, Depends(services)]
User = Annotated[str, Depends(current_user)]
LangQ = Annotated[str, Depends(language)]
