"""
Global test configuration and database fixture for PR Sentinel test suite.
Automatically configures SQLite async database when running unit/integration tests.
"""
import asyncio
import os
import pytest
from app.config import settings
from app.db.models import Base
from app.db.session import get_engine


@pytest.fixture(autouse=True)
def setup_test_sqlite_db():
    test_db = "sqlite+aiosqlite:///./test_pr_sentinel.db"
    settings.DATABASE_URL = test_db
    engine = get_engine(test_db)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _cleanup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    asyncio.run(_init())
    yield
    asyncio.run(_cleanup())
    if os.path.exists("./test_pr_sentinel.db"):
        try:
            os.remove("./test_pr_sentinel.db")
        except Exception:
            pass

