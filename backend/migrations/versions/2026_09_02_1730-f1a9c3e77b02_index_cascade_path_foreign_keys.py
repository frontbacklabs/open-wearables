"""index the foreign keys on the user-delete cascade path

Postgres indexes the *parent* side of a foreign key, never the child, so a referencing
column with no index of its own forces a sequential scan of the child table for every
parent row deleted. Deleting one user fans out — user -> data_source -> health_score,
user -> user_connection -> data_source — and each unindexed hop repeats a full scan,
which is what pushed DELETE /users/{id} past statement_timeout in production.

Only the columns on that cascade path are covered here. The remaining unindexed FKs
(api_key.created_by, application.developer_id, invitation.invited_by_id,
user_invitation_code.created_by_id, data_point_series.series_type_definition_id) all
point at developer or series_type_definition, which are not deleted in normal operation.

Built CONCURRENTLY: a plain CREATE INDEX takes a lock that blocks writes to the table for
the duration, and health_score is large enough in production for that to be an outage.
CONCURRENTLY cannot run inside a transaction, hence the autocommit blocks below — which
also means a failed build leaves an INVALID index behind that must be dropped before
retrying, rather than rolling back.

Revision ID: f1a9c3e77b02
Revises: ca47df2312b7

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a9c3e77b02"
down_revision: Union[str, None] = "ca47df2312b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES: tuple[tuple[str, str, str], ...] = (
    # ON DELETE CASCADE: scanned once per data_source removed with a user.
    ("ix_health_score_data_source_id", "health_score", "data_source_id"),
    # ON DELETE SET NULL: scanned once per user_connection removed with a user.
    ("ix_data_source_user_connection_id", "data_source", "user_connection_id"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, column in _INDEXES:
            op.execute(f'CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON "{table}" ({column})')


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _column in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
