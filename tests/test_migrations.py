"""
Tests for Alembic database migrations and schema consistency in PR Sentinel.
Verifies:
1. Complete migration chain from base to head (and downgrade).
2. Schema parity between SQLAlchemy ReviewJob model and Alembic migration state.
3. Every required column, index, and constraint exists on the table.
"""
import os
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.db.models import Base, ReviewJob


def test_migration_chain_and_schema_parity(tmp_path):
    """
    Applies all migrations forward to head against a test SQLite database,
    inspects table columns, verifies parity with SQLAlchemy ReviewJob model,
    and tests full downgrade.
    """
    test_db_path = tmp_path / "test_migration.db"
    async_db_url = f"sqlite+aiosqlite:///{test_db_path.as_posix()}"
    sync_db_url = f"sqlite:///{test_db_path.as_posix()}"

    # Setup Alembic Config pointing to project's alembic.ini
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", async_db_url)

    # 1. Upgrade from base to head
    command.upgrade(alembic_cfg, "head")

    # 2. Inspect created table using SQLAlchemy Inspector
    engine = create_engine(sync_db_url)
    inspector = inspect(engine)

    tables = inspector.get_table_names()
    assert "review_jobs" in tables, "review_jobs table must exist after upgrade head"

    # Get column names from database
    db_columns = {col["name"]: col for col in inspector.get_columns("review_jobs")}

    # Get expected column names from ReviewJob model
    model_columns = {col.name: col for col in ReviewJob.__table__.columns}

    # Verify every column in the SQLAlchemy model exists in the migrated database
    for col_name in model_columns:
        assert col_name in db_columns, f"Column '{col_name}' missing from migrations"

    # Specifically verify the newest columns exist
    assert "summary" in db_columns, "Column 'summary' must exist in migrated table"
    assert "findings_data" in db_columns, "Column 'findings_data' must exist in migrated table"
    assert "worker_id" in db_columns
    assert "lease_expires_at" in db_columns
    assert "next_retry_at" in db_columns
    assert "final_verdict" in db_columns
    assert "findings_count" in db_columns
    assert "github_review_id" in db_columns

    # Verify indexes
    db_indexes = {idx["name"] for idx in inspector.get_indexes("review_jobs")}
    assert "ix_review_jobs_claim_lookup" in db_indexes
    assert "ix_review_jobs_delivery_id" in db_indexes

    # 3. Test downgrade to base
    command.downgrade(alembic_cfg, "base")
    inspector_downgraded = inspect(engine)
    assert "review_jobs" not in inspector_downgraded.get_table_names(), (
        "review_jobs table should be dropped after downgrade to base"
    )

    # 4. Re-upgrade to head to ensure repeatable migrations
    command.upgrade(alembic_cfg, "head")
    inspector_reupgraded = inspect(engine)
    assert "review_jobs" in inspector_reupgraded.get_table_names()
