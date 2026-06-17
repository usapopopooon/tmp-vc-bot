"""Add configurable lobby settings.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-17 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add non-destructive settings for existing and future lobbies."""
    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.add_column(
            sa.Column(
                "naming_mode",
                sa.String(),
                nullable=False,
                server_default="personal",
            )
        )
        batch_op.add_column(sa.Column("room_prefix", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "number_style",
                sa.String(),
                nullable=False,
                server_default="half",
            )
        )
        batch_op.add_column(
            sa.Column(
                "number_match_mode",
                sa.String(),
                nullable=False,
                server_default="both",
            )
        )
        batch_op.add_column(
            sa.Column("start_number", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("owner_mode", sa.String(), nullable=False, server_default="owner")
        )
        batch_op.add_column(
            sa.Column(
                "control_policy",
                sa.String(),
                nullable=False,
                server_default="owner",
            )
        )
        for column_name in (
            "allow_rename",
            "allow_limit",
            "allow_bitrate",
            "allow_region",
            "allow_lock",
            "allow_hide",
            "allow_nsfw",
            "allow_transfer",
            "allow_kick",
            "allow_dissolve",
            "allow_block",
            "allow_allow",
            "allow_camera",
        ):
            batch_op.add_column(
                sa.Column(
                    column_name,
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.true(),
                )
            )

    with op.batch_alter_table("voice_sessions") as batch_op:
        batch_op.alter_column("owner_id", existing_type=sa.String(), nullable=True)
        batch_op.add_column(sa.Column("sequence_number", sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_lobby_sequence",
            ["lobby_id", "sequence_number"],
        )


def downgrade() -> None:
    """Remove configurable lobby settings."""
    op.execute("UPDATE voice_sessions SET owner_id = '0' WHERE owner_id IS NULL")
    with op.batch_alter_table("voice_sessions") as batch_op:
        batch_op.drop_constraint("uq_lobby_sequence", type_="unique")
        batch_op.drop_column("sequence_number")
        batch_op.alter_column("owner_id", existing_type=sa.String(), nullable=False)

    with op.batch_alter_table("lobbies") as batch_op:
        for column_name in (
            "allow_camera",
            "allow_allow",
            "allow_block",
            "allow_dissolve",
            "allow_kick",
            "allow_transfer",
            "allow_nsfw",
            "allow_hide",
            "allow_lock",
            "allow_region",
            "allow_bitrate",
            "allow_limit",
            "allow_rename",
        ):
            batch_op.drop_column(column_name)
        batch_op.drop_column("control_policy")
        batch_op.drop_column("owner_mode")
        batch_op.drop_column("start_number")
        batch_op.drop_column("number_match_mode")
        batch_op.drop_column("number_style")
        batch_op.drop_column("room_prefix")
        batch_op.drop_column("naming_mode")
