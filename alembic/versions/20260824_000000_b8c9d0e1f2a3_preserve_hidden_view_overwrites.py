"""Preserve channel view overwrites while a voice channel is hidden.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-24 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the original view overwrite snapshot to active voice sessions."""
    with op.batch_alter_table("voice_sessions") as batch_op:
        batch_op.add_column(
            sa.Column("hidden_view_overwrites", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    """Remove the original view overwrite snapshot."""
    with op.batch_alter_table("voice_sessions") as batch_op:
        batch_op.drop_column("hidden_view_overwrites")
