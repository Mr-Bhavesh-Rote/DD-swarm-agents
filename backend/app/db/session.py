"""Async + sync DB engines/sessions.

The FastAPI app uses the async engine. The background worker runs the LangGraph workflow
synchronously (LangGraph nodes here are sync) and uses a sync engine/session derived from
the same DATABASE_URL.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _sync_url(url: str) -> str:
    # postgresql+asyncpg://...  ->  postgresql+psycopg://...
    return url.replace("+asyncpg", "+psycopg")


_settings = get_settings()

async_engine = create_async_engine(_settings.database_url, pool_pre_ping=True, future=True)
AsyncSessionLocal = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

sync_engine = create_engine(_sync_url(_settings.database_url), pool_pre_ping=True, future=True)
SyncSessionLocal = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session."""
    async with AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def async_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


@contextmanager
def sync_session() -> Iterator[Session]:
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()
