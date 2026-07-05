"""Add cross-guild voice notification excludes.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-06 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create cross-guild voice notification exclude table."""
    op.create_table(
        "voice_notify_cross_guild_excludes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("voice_channel_id", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "guild_id",
            "voice_channel_id",
            name="uq_voice_notify_cross_guild_exclude_guild_voice_channel",
        ),
    )
    op.create_index(
        "ix_voice_notify_cross_guild_excludes_guild_id",
        "voice_notify_cross_guild_excludes",
        ["guild_id"],
    )
    op.execute(
        """
        INSERT INTO voice_notify_cross_guild_excludes (
            guild_id,
            voice_channel_id,
            created_at,
            updated_at
        )
        SELECT
            guild_id,
            voice_channel_id,
            created_at,
            updated_at
        FROM voice_notify_excludes
        """
    )


def downgrade() -> None:
    """Drop cross-guild voice notification exclude table."""
    op.drop_index(
        "ix_voice_notify_cross_guild_excludes_guild_id",
        "voice_notify_cross_guild_excludes",
    )
    op.drop_table("voice_notify_cross_guild_excludes")
