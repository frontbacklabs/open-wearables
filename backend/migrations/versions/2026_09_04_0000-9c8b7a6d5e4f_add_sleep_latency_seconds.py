"""add sleep latency seconds

Revision ID: 9c8b7a6d5e4f
Revises: f1a9c3e77b02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9c8b7a6d5e4f"
down_revision: Union[str, None] = "f1a9c3e77b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sleep_details", sa.Column("sleep_latency_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sleep_details", "sleep_latency_seconds")
