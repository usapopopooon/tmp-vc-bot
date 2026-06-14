"""Drop voice_sessions.user_limit.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-14 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Remove cached per-session user limit from the database."""
    with op.batch_alter_table("voice_sessions") as batch_op:
        batch_op.drop_column("user_limit")


def downgrade() -> None:
    """Restore cached per-session user limit."""
    with op.batch_alter_table("voice_sessions") as batch_op:
        batch_op.add_column(sa.Column("user_limit", sa.Integer(), nullable=True))
