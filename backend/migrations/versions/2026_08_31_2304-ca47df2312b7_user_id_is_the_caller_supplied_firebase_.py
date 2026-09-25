"""user id is the caller-supplied firebase uid

Open Wearables users are now a one-to-one mirror of the Ren user table, keyed on the
same id. Ren ids are Firebase UIDs (text), so ``user.id`` and every column referencing
it move from UUID to VARCHAR(255).

There is no value-preserving conversion: an existing UUID is not a Firebase UID, so a
cast would leave rows that mirror nothing. Every user and all user-scoped data is
deleted instead, and the mirror is repopulated from Ren. This is a deliberate,
sanctioned wipe of user data — it is NOT reversible, and the downgrade restores the
column types only, not the rows.

Revision ID: ca47df2312b7
Revises: dc5ac28c4b94

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ca47df2312b7"
down_revision: Union[str, None] = "dc5ac28c4b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, constraint name, nullable) for every FK onto user.id.
_USER_FKS: tuple[tuple[str, str, str, bool], ...] = (
    ("data_source", "user_id", "data_source_user_id_fkey", False),
    ("health_score", "user_id", "health_score_user_id_fkey", False),
    ("personal_record", "user_id", "personal_record_user_id_fkey", False),
    ("refresh_token", "user_id", "refresh_token_user_id_fkey", True),
    ("user_connection", "user_id", "user_connection_user_id_fkey", False),
    ("user_invitation_code", "user_id", "user_invitation_code_user_id_fkey", False),
)


def _swap_types(from_type: sa.types.TypeEngine, to_type: sa.types.TypeEngine, using: str) -> None:
    """Retype user.id and its referencing columns together.

    Postgres refuses to leave a foreign key whose two sides have different types, so the
    constraints come off first and go back on once every column has moved. All of it runs
    in the one transaction alembic wraps around the migration.
    """
    for table, column, constraint, _ in _USER_FKS:
        op.drop_constraint(constraint, table, type_="foreignkey")

    op.alter_column(
        "user",
        "id",
        existing_type=from_type,
        type_=to_type,
        existing_nullable=False,
        postgresql_using=f"id{using}",
    )
    for table, column, _, nullable in _USER_FKS:
        op.alter_column(
            table,
            column,
            existing_type=from_type,
            type_=to_type,
            existing_nullable=nullable,
            postgresql_using=f"{column}{using}",
        )

    for table, column, constraint, _ in _USER_FKS:
        op.create_foreign_key(constraint, table, "user", [column], ["id"], ondelete="CASCADE")


def upgrade() -> None:
    # Deleting users cascades to every table in _USER_FKS and onward through their own
    # cascades, so this clears all user-scoped data before the columns are retyped.
    op.execute('DELETE FROM "user"')

    _swap_types(sa.UUID(), sa.String(length=255), using="::text")


def downgrade() -> None:
    # The rows are gone; only the shape comes back. Anything still present would have to
    # be a valid UUID to survive the cast, which a mirrored Firebase UID never is.
    op.execute('DELETE FROM "user"')

    _swap_types(sa.String(length=255), sa.UUID(), using="::uuid")
