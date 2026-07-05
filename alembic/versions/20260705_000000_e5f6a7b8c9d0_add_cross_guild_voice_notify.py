"""Add cross-guild voice notification settings.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create cross-guild voice notification configuration table."""
    op.create_table(
        "voice_notify_cross_guild_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column(
            "share_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("notify_channel_id", sa.String(), nullable=True),
        sa.Column("invite_url", sa.String(), nullable=True),
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
            name="uq_voice_notify_cross_guild_guild",
        ),
    )
    op.create_index(
        "ix_voice_notify_cross_guild_configs_guild_id",
        "voice_notify_cross_guild_configs",
        ["guild_id"],
    )
    op.create_index(
        "ix_voice_notify_cross_guild_configs_notify_channel_id",
        "voice_notify_cross_guild_configs",
        ["notify_channel_id"],
    )


def downgrade() -> None:
    """Drop cross-guild voice notification configuration table."""
    op.drop_index(
        "ix_voice_notify_cross_guild_configs_notify_channel_id",
        "voice_notify_cross_guild_configs",
    )
    op.drop_index(
        "ix_voice_notify_cross_guild_configs_guild_id",
        "voice_notify_cross_guild_configs",
    )
    op.drop_table("voice_notify_cross_guild_configs")
