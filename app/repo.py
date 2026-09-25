"""Workspace-scoped data access. Handlers receive a `Scoped` built from the
authenticated session and never issue unscoped queries for tenant-owned rows."""
from __future__ import annotations

import uuid
from typing import TypeVar

from sqlalchemy import Select, delete, select, update
from sqlalchemy.orm import Session

T = TypeVar('T')


def as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


class Scoped:
    def __init__(self, s: Session, workspace_id: uuid.UUID):
        if not isinstance(workspace_id, uuid.UUID):
            raise TypeError('workspace_id must come from the authenticated session')
        self.s = s
        self.wid = workspace_id

    def q(self, model: type[T], *columns) -> Select:
        stmt = select(*columns) if columns else select(model)
        return stmt.where(model.workspace_id == self.wid)

    def get(self, model: type[T], ident) -> T | None:
        ident = as_uuid(ident)
        if ident is None:
            return None
        return self.s.scalars(self.q(model).where(model.id == ident)).first()

    def all(self, stmt: Select) -> list:
        return list(self.s.scalars(stmt))

    def first(self, stmt: Select):
        return self.s.scalars(stmt).first()

    def add(self, obj):
        obj.workspace_id = self.wid
        self.s.add(obj)
        return obj

    def update(self, model, *where, **values) -> int:
        return self.s.execute(update(model).where(model.workspace_id == self.wid, *where).values(**values)).rowcount

    def delete(self, model, *where) -> int:
        return self.s.execute(delete(model).where(model.workspace_id == self.wid, *where)).rowcount
