"""Add voice join and leave notification settings.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-30 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create voice notification configuration tables."""
    op.create_table(
        "voice_notify_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("voice_channel_id", sa.String(), nullable=False),
        sa.Column("notify_channel_id", sa.String(), nullable=False),
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
            name="uq_voice_notify_guild_voice_channel",
        ),
    )
    op.create_index(
        "ix_voice_notify_configs_guild_id",
        "voice_notify_configs",
        ["guild_id"],
    )
    op.create_index(
        "ix_voice_notify_configs_notify_channel_id",
        "voice_notify_configs",
        ["notify_channel_id"],
    )

    op.create_table(
        "voice_notify_category_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("category_id", sa.String(), nullable=False),
        sa.Column("notify_channel_id", sa.String(), nullable=False),
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
            "category_id",
            name="uq_voice_notify_category_guild_category",
        ),
    )
    op.create_index(
        "ix_voice_notify_category_configs_guild_id",
        "voice_notify_category_configs",
        ["guild_id"],
    )
    op.create_index(
        "ix_voice_notify_category_configs_notify_channel_id",
        "voice_notify_category_configs",
        ["notify_channel_id"],
    )

    op.create_table(
        "voice_notify_excludes",
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
            name="uq_voice_notify_exclude_guild_voice_channel",
        ),
    )
    op.create_index(
        "ix_voice_notify_excludes_guild_id",
        "voice_notify_excludes",
        ["guild_id"],
    )


def downgrade() -> None:
    """Drop voice notification configuration tables."""
    op.drop_index("ix_voice_notify_excludes_guild_id", "voice_notify_excludes")
    op.drop_table("voice_notify_excludes")

    op.drop_index(
        "ix_voice_notify_category_configs_notify_channel_id",
        "voice_notify_category_configs",
    )
    op.drop_index(
        "ix_voice_notify_category_configs_guild_id",
        "voice_notify_category_configs",
    )
    op.drop_table("voice_notify_category_configs")

    op.drop_index(
        "ix_voice_notify_configs_notify_channel_id",
        "voice_notify_configs",
    )
    op.drop_index("ix_voice_notify_configs_guild_id", "voice_notify_configs")
    op.drop_table("voice_notify_configs")
