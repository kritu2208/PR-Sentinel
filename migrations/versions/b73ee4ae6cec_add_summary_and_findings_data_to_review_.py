"""add_summary_and_findings_data_to_review_jobs

Revision ID: b73ee4ae6cec
Revises: fe5c1dc21897
Create Date: 2026-09-08 16:44:29.873957

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b73ee4ae6cec'
down_revision: Union[str, Sequence[str], None] = 'fe5c1dc21897'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('review_jobs', sa.Column('summary', sa.Text(), nullable=True))
    op.add_column('review_jobs', sa.Column('findings_data', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('review_jobs', 'findings_data')
    op.drop_column('review_jobs', 'summary')
