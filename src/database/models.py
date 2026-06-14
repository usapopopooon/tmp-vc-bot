"""SQLAlchemy database models.

データベースのテーブル構造を Python クラスで定義する。

テーブル構成:
    - lobbies: ロビーVC の設定 (どのチャンネルがロビーか)
    - voice_sessions: 現在アクティブな一時 VC のセッション情報
    - voice_session_members: 一時 VC の参加メンバー
    - processed_events: マルチインスタンス重複排除テーブル
"""

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    validates,
)


def _validate_discord_id(value: str, field_name: str) -> str:
    """Discord ID (数字文字列) のバリデーション。"""
    if not isinstance(value, str) or not value.isdigit():
        msg = f"{field_name} must be a digit string, got: {value!r}"
        raise ValueError(msg)
    return value


class Base(DeclarativeBase):
    """全モデルの基底クラス。"""

    pass


class Lobby(Base):
    """ロビーVC の設定テーブル。

    ロビーVC = ユーザーが参加すると一時 VC が自動作成されるチャンネル。
    """

    __tablename__ = "lobbies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    lobby_channel_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    category_id: Mapped[str | None] = mapped_column(String, nullable=True)
    default_user_limit: Mapped[int] = mapped_column(Integer, default=0)

    sessions: Mapped[list["VoiceSession"]] = relationship(
        "VoiceSession", back_populates="lobby", cascade="all, delete-orphan"
    )

    @validates("guild_id", "lobby_channel_id")
    def _validate_ids(self, key: str, value: str) -> str:
        return _validate_discord_id(value, key)

    def __repr__(self) -> str:
        return (
            f"<Lobby(id={self.id}, guild_id={self.guild_id}, "
            f"channel_id={self.lobby_channel_id})>"
        )


class VoiceSession(Base):
    """現在アクティブな一時 VC のセッション情報テーブル。

    ユーザーがロビーに参加するとレコードが作成され、
    全員が退出するとレコードが削除される。
    """

    __tablename__ = "voice_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lobby_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lobbies.id"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    lobby: Mapped["Lobby"] = relationship("Lobby", back_populates="sessions")

    @validates("channel_id", "owner_id")
    def _validate_ids(self, key: str, value: str) -> str:
        return _validate_discord_id(value, key)

    def __repr__(self) -> str:
        return (
            f"<VoiceSession(id={self.id}, channel_id={self.channel_id}, "
            f"owner_id={self.owner_id})>"
        )


class VoiceSessionMember(Base):
    """一時 VC に参加しているメンバーの情報テーブル。

    各メンバーの参加時刻を記録し、オーナー引き継ぎ時の優先順位を決定する。
    """

    __tablename__ = "voice_session_members"
    __table_args__ = (
        UniqueConstraint("voice_session_id", "user_id", name="uq_session_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voice_session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("voice_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<VoiceSessionMember(id={self.id}, session_id={self.voice_session_id}, "
            f"user_id={self.user_id}, joined_at={self.joined_at})>"
        )


class ProcessedEvent(Base):
    """重複排除テーブル (マルチインスタンス重複防止)。

    複数インスタンスが同じ Discord Gateway イベントを受信した際に、
    1 インスタンスだけが処理を実行するための重複排除レコード。
    """

    __tablename__ = "processed_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ProcessedEvent(id={self.id}, event_key={self.event_key})>"
