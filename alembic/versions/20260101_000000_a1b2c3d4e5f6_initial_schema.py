"""Initial schema for tmp-vc-bot.

Creates: lobbies, voice_sessions, voice_session_members, processed_events.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-01-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all tables."""
    # lobbies: ロビー VC の設定
    op.create_table(
        "lobbies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("guild_id", sa.String(), nullable=False),
        sa.Column("lobby_channel_id", sa.String(), nullable=False),
        sa.Column("category_id", sa.String(), nullable=True),
        sa.Column("default_user_limit", sa.Integer(), nullable=True, default=0),
    )
    op.create_index("ix_lobbies_guild_id", "lobbies", ["guild_id"])
    op.create_index(
        "ix_lobbies_lobby_channel_id",
        "lobbies",
        ["lobby_channel_id"],
        unique=True,
    )

    # voice_sessions: 一時 VC のセッション情報
    op.create_table(
        "voice_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "lobby_id",
            sa.Integer(),
            sa.ForeignKey("lobbies.id"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("user_limit", sa.Integer(), nullable=True, default=0),
        sa.Column(
            "is_locked",
            sa.Boolean(),
            nullable=True,
            default=False,
        ),
        sa.Column(
            "is_hidden",
            sa.Boolean(),
            nullable=True,
            default=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_voice_sessions_channel_id",
        "voice_sessions",
        ["channel_id"],
        unique=True,
    )

    # voice_session_members: 一時 VC の参加メンバー
    op.create_table(
        "voice_session_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "voice_session_id",
            sa.Integer(),
            sa.ForeignKey("voice_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "voice_session_id", "user_id", name="uq_session_user"
        ),
    )
    op.create_index(
        "ix_voice_session_members_user_id",
        "voice_session_members",
        ["user_id"],
    )

    # processed_events: マルチインスタンス重複排除テーブル
    op.create_table(
        "processed_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_key", sa.String(), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table("processed_events")
    op.drop_index("ix_voice_session_members_user_id", "voice_session_members")
    op.drop_table("voice_session_members")
    op.drop_index("ix_voice_sessions_channel_id", "voice_sessions")
    op.drop_table("voice_sessions")
    op.drop_index("ix_lobbies_lobby_channel_id", "lobbies")
    op.drop_index("ix_lobbies_guild_id", "lobbies")
    op.drop_table("lobbies")
