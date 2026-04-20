"""CRUD operations for alert_rules and alert_events tables."""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import and_, delete, desc, insert, select, update

from .api import check_db_connected
from .base import DB
from .models import AlertEvents, AlertRules


# ─── AlertRules CRUD ──────────────────────────────────────────────────────────

@check_db_connected
async def list_rules(user_id: str) -> List[dict]:
    query = select(AlertRules).where(AlertRules.c.user_id == user_id)
    db = DB.get()
    async with db.transaction():
        rows = await db.fetch_all(query)
    return [dict(r) for r in rows]


@check_db_connected
async def list_active_rules() -> List[dict]:
    """Utilisé par l'evaluator, sans filtre user_id."""
    query = select(AlertRules).where(AlertRules.c.is_active == True)
    db = DB.get()
    async with db.transaction():
        rows = await db.fetch_all(query)
    return [dict(r) for r in rows]


@check_db_connected
async def get_rule(rule_id: int, user_id: str) -> Optional[dict]:
    query = select(AlertRules).where(
        and_(AlertRules.c.id == rule_id, AlertRules.c.user_id == user_id)
    )
    db = DB.get()
    async with db.transaction():
        row = await db.fetch_one(query)
    return dict(row) if row else None


@check_db_connected
async def create_rule(user_id: str, data: dict) -> dict:
    values = {
        "user_id": user_id,
        "is_active": True,
        "created_at": datetime.utcnow(),
        **data,
    }
    db = DB.get()
    async with db.transaction():
        rule_id = await db.execute(insert(AlertRules).values(**values))
    return await get_rule(rule_id, user_id)


@check_db_connected
async def update_rule(rule_id: int, user_id: str, data: dict) -> Optional[dict]:
    query = (
        update(AlertRules)
        .where(and_(AlertRules.c.id == rule_id, AlertRules.c.user_id == user_id))
        .values(**data)
    )
    db = DB.get()
    async with db.transaction():
        await db.execute(query)
    return await get_rule(rule_id, user_id)


@check_db_connected
async def delete_rule(rule_id: int, user_id: str) -> int:
    query = delete(AlertRules).where(
        and_(AlertRules.c.id == rule_id, AlertRules.c.user_id == user_id)
    )
    db = DB.get()
    async with db.transaction():
        return await db.execute(query)


@check_db_connected
async def toggle_rule(rule_id: int, user_id: str) -> Optional[dict]:
    current = await get_rule(rule_id, user_id)
    if not current:
        return None
    new_state = not current["is_active"]
    query = (
        update(AlertRules)
        .where(and_(AlertRules.c.id == rule_id, AlertRules.c.user_id == user_id))
        .values(is_active=new_state)
    )
    db = DB.get()
    async with db.transaction():
        await db.execute(query)
    return {"is_active": new_state}


# ─── AlertEvents CRUD ─────────────────────────────────────────────────────────

@check_db_connected
async def create_event(data: dict) -> int:
    values = {
        "triggered_at": datetime.utcnow(),
        "resolved_at": None,
        "is_resolved": False,
        **data,
    }
    db = DB.get()
    async with db.transaction():
        return await db.execute(insert(AlertEvents).values(**values))


@check_db_connected
async def create_event_if_no_firing(data: dict) -> Optional[int]:
    db = DB.get()
    async with db.transaction():
        check_query = (
            select(AlertEvents.c.id)
            .where(and_(
                AlertEvents.c.rule_id == data["rule_id"],
                AlertEvents.c.is_resolved == False,
            ))
            .with_for_update()
        )
        existing = await db.fetch_one(check_query)
        if existing:
            return None

        values = {
            "triggered_at": datetime.utcnow(),
            "resolved_at": None,
            "is_resolved": False,
            **data,
        }
        return await db.execute(insert(AlertEvents).values(**values))


@check_db_connected
async def resolve_events_for_rule(rule_id: int) -> None:
    """Résout automatiquement tous les events actifs d'une règle."""
    query = (
        update(AlertEvents)
        .where(and_(AlertEvents.c.rule_id == rule_id, AlertEvents.c.is_resolved == False))
        .values(is_resolved=True, resolved_at=datetime.utcnow())
    )
    db = DB.get()
    async with db.transaction():
        await db.execute(query)


@check_db_connected
async def list_active_events(user_id: str) -> List[dict]:
    query = (
        select(AlertEvents)
        .where(and_(AlertEvents.c.user_id == user_id, AlertEvents.c.is_resolved == False))
        .order_by(desc(AlertEvents.c.triggered_at))
    )
    db = DB.get()
    async with db.transaction():
        rows = await db.fetch_all(query)
    return [dict(r) for r in rows]


@check_db_connected
async def list_history(user_id: str, limit: int = 50) -> List[dict]:
    query = (
        select(AlertEvents)
        .where(AlertEvents.c.user_id == user_id)
        .order_by(desc(AlertEvents.c.triggered_at))
        .limit(limit)
    )
    db = DB.get()
    async with db.transaction():
        rows = await db.fetch_all(query)
    return [dict(r) for r in rows]


@check_db_connected
async def resolve_event(event_id: int, user_id: str) -> bool:
    query = (
        update(AlertEvents)
        .where(and_(AlertEvents.c.id == event_id, AlertEvents.c.user_id == user_id))
        .values(is_resolved=True, resolved_at=datetime.utcnow())
    )
    db = DB.get()
    async with db.transaction():
        result = await db.execute(query)
    return result > 0