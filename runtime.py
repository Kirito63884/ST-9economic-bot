"""Durable transactions shared by workers; never store Discord tokens in game state."""

import asyncio
import secrets
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import AppRuntime, SessionLocal, engine, run_migrations

current_db = ContextVar("current_db", default=None)
current_runtime = ContextVar("current_runtime", default=None)
transaction_lock = asyncio.Lock()


async def finish_thread(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Do not close/roll back a session still in use by its worker thread.
        await task
        raise


def initialize_database():
    run_migrations()
    with SessionLocal() as db:
        row = db.get(AppRuntime, 1)
        if row is None:
            db.add(AppRuntime(id=1, payload="{}", secret_key=secrets.token_hex(32)))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()  # Another worker initialized the same row.
        return db.get(AppRuntime, 1).secret_key


def lock_runtime_row(db):
    if engine.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    return db.query(AppRuntime).filter(AppRuntime.id == 1).with_for_update().one()


@asynccontextmanager
async def transaction(serialize, restore):
    async with transaction_lock:
        db = SessionLocal()
        db_token = row_token = None
        before = None
        try:
            # Waiting for another worker's DB lock must not block the event loop.
            row = await finish_thread(lock_runtime_row, db)
            before = row.payload
            restore(before)
            db_token = current_db.set(db)
            row_token = current_runtime.set(row)
            yield db
            payload = serialize()
            if payload != before:
                row.payload = payload
            await finish_thread(db.commit)
        except BaseException:
            db.rollback()
            if before is not None:
                restore(before)
            raise
        finally:
            if row_token is not None:
                current_runtime.reset(row_token)
            if db_token is not None:
                current_db.reset(db_token)
            db.close()


@asynccontextmanager
async def metadata_transaction():
    """Bot changes share the website's lock without loading or changing its games."""
    async with transaction(lambda: current_runtime.get().payload, lambda _: None) as db:
        yield db
