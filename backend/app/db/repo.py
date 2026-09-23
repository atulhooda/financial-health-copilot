"""User-scoped data access. Every query goes through UserRepo, which always filters by user_id (rule 3)."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import USER_SCOPED_MODELS


class UserRepo:
    def __init__(self, session: Session, user_id: str):
        if not user_id:
            raise ValueError("user_id is required")
        self.s = session
        self.user_id = user_id

    def select(self, model, *where, order_by=()):
        stmt = select(model).where(model.user_id == self.user_id, *where)
        if order_by:
            stmt = stmt.order_by(*order_by)
        return list(self.s.scalars(stmt))

    def get(self, model, **pk):
        return self.s.get(model, {"user_id": self.user_id, **pk})

    def add(self, obj) -> None:
        if getattr(obj, "user_id", None) != self.user_id:
            raise ValueError("row user_id does not match repo user_id")
        self.s.add(obj)

    def add_all(self, objs) -> None:
        for o in objs:
            self.add(o)

    def delete_where(self, model, *where) -> int:
        res = self.s.execute(delete(model).where(model.user_id == self.user_id, *where))
        return res.rowcount or 0

    def erase_all(self) -> dict[str, int]:
        """DPDP erasure (SPEC D14): every user-scoped table, children first."""
        counts = {}
        for model in reversed(USER_SCOPED_MODELS):
            counts[model.__tablename__] = self.delete_where(model)
        return counts
