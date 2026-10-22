"""refresh token reuse window

Adds refresh_token.rotated_to_id: the successor a token was rotated into. Lets
the refresh endpoint re-present the current live pair when a client retries with
a recently rotated token (lost refresh response), instead of orphaning the
credential chain with a 401.

Revision ID: f3063e4ba61d
Revises: 19248cba05f6

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3063e4ba61d"
down_revision: Union[str, None] = "19248cba05f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_token", sa.Column("rotated_to_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("refresh_token", "rotated_to_id")
