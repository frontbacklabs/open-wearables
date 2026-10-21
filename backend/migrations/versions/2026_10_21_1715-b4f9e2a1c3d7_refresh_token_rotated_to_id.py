"""refresh_token.rotated_to_id

Record the successor issued during refresh-token rotation so a replayed
rotated-out token inside the grace window can be answered with the already-issued
successor instead of a hard 401 (lost refresh responses orphaned mobile clients
permanently).

Revision ID: b4f9e2a1c3d7
Revises: 19248cba05f6

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4f9e2a1c3d7"
down_revision: Union[str, None] = "19248cba05f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("rotated_to_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("refresh_token", "rotated_to_id")
