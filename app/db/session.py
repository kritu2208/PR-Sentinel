"""
Database session and engine management for PR Sentinel.
"""
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine(db_url: str | None = None) -> AsyncEngine:
    """Returns or creates the global AsyncEngine instance."""
    global _engine, _session_factory
    url = db_url or settings.DATABASE_URL
    if _engine is None or str(_engine.url) != url:
        # SQLite needs specific connect_args for multithreading/concurrency
        connect_args = {}
        if "sqlite" in url:
            connect_args = {"check_same_thread": False}

        _engine = create_async_engine(
            url,
            echo=False,
            future=True,
            poolclass=NullPool,
            connect_args=connect_args,
        )
        _session_factory = async_sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _engine


def get_session_factory(db_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    """Returns the async_sessionmaker instance."""
    global _session_factory
    if _session_factory is None or db_url is not None:
        get_engine(db_url)
    return _session_factory  # type: ignore[return-value]


@asynccontextmanager
async def get_session(db_url: str | None = None) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for yielding an AsyncSession."""
    factory = get_session_factory(db_url)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db(engine: AsyncEngine | None = None) -> None:
    """Creates database tables (primarily used for test harness setup)."""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db(engine: AsyncEngine | None = None) -> None:
    """Drops database tables (primarily used for test harness teardown)."""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
