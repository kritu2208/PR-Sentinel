"""add_review_job_result_fields

Revision ID: 6dc3415dba2d
Revises: 05645529560d
Create Date: 2026-09-05 23:30:41.597686

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6dc3415dba2d'
down_revision: Union[str, Sequence[str], None] = '05645529560d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("review_jobs", sa.Column("final_verdict", sa.String(length=32), nullable=True))
    op.add_column("review_jobs", sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("review_jobs", sa.Column("github_review_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("review_jobs", "github_review_id")
    op.drop_column("review_jobs", "findings_count")
    op.drop_column("review_jobs", "final_verdict")
