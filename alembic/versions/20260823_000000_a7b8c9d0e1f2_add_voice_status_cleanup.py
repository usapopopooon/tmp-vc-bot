"""Add category voice status cleanup settings.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-23 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create category voice status cleanup settings table."""
    op.create_table(
        "voice_status_cleanup_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("category_id", sa.String(), nullable=False),
        sa.Column(
            "delay_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("300"),
        ),
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
        sa.CheckConstraint(
            "delay_seconds BETWEEN 60 AND 86400",
            name="ck_voice_status_cleanup_delay_seconds",
        ),
        sa.UniqueConstraint(
            "guild_id",
            "category_id",
            name="uq_voice_status_cleanup_guild_category",
        ),
    )
    op.create_index(
        "ix_voice_status_cleanup_configs_guild_id",
        "voice_status_cleanup_configs",
        ["guild_id"],
    )


def downgrade() -> None:
    """Drop category voice status cleanup settings table."""
    op.drop_index(
        "ix_voice_status_cleanup_configs_guild_id",
        "voice_status_cleanup_configs",
    )
    op.drop_table("voice_status_cleanup_configs")
