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
    Index,
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

from src.core.lobby_config import (
    LOBBY_CONTROL_OWNER,
    LOBBY_NAMING_PERSONAL,
    LOBBY_OWNER_MODE_OWNER,
    NUMBER_MATCH_BOTH,
    NUMBER_STYLE_HALF,
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
    naming_mode: Mapped[str] = mapped_column(
        String, nullable=False, default=LOBBY_NAMING_PERSONAL
    )
    room_prefix: Mapped[str | None] = mapped_column(String, nullable=True)
    number_style: Mapped[str] = mapped_column(
        String, nullable=False, default=NUMBER_STYLE_HALF
    )
    number_match_mode: Mapped[str] = mapped_column(
        String, nullable=False, default=NUMBER_MATCH_BOTH
    )
    start_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner_mode: Mapped[str] = mapped_column(
        String, nullable=False, default=LOBBY_OWNER_MODE_OWNER
    )
    control_policy: Mapped[str] = mapped_column(
        String, nullable=False, default=LOBBY_CONTROL_OWNER
    )
    allow_rename: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_bitrate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_region: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_lock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_hide: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_nsfw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_transfer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_kick: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_dissolve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_block: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_allow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_camera: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

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
    __table_args__ = (
        UniqueConstraint("lobby_id", "sequence_number", name="uq_lobby_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lobby_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("lobbies.id"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    sequence_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    lobby: Mapped["Lobby"] = relationship("Lobby", back_populates="sessions")

    @validates("channel_id", "owner_id")
    def _validate_ids(self, key: str, value: str | None) -> str | None:
        if key == "owner_id" and value is None:
            return value
        if value is None:
            msg = f"{key} must be a digit string, got: {value!r}"
            raise ValueError(msg)
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


class VoiceNotifyConfig(Base):
    """VC 単位の入退室通知設定テーブル。"""

    __tablename__ = "voice_notify_configs"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "voice_channel_id",
            name="uq_voice_notify_guild_voice_channel",
        ),
        Index("ix_voice_notify_configs_guild_id", "guild_id"),
        Index("ix_voice_notify_configs_notify_channel_id", "notify_channel_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[str] = mapped_column(String, nullable=False)
    voice_channel_id: Mapped[str] = mapped_column(String, nullable=False)
    notify_channel_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    @validates("guild_id", "voice_channel_id", "notify_channel_id")
    def _validate_ids(self, key: str, value: str) -> str:
        return _validate_discord_id(value, key)

    def __repr__(self) -> str:
        return (
            f"<VoiceNotifyConfig(id={self.id}, guild_id={self.guild_id}, "
            f"voice_channel_id={self.voice_channel_id}, "
            f"notify_channel_id={self.notify_channel_id})>"
        )


class VoiceNotifyCategoryConfig(Base):
    """カテゴリ単位の VC 入退室通知設定テーブル。"""

    __tablename__ = "voice_notify_category_configs"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "category_id",
            name="uq_voice_notify_category_guild_category",
        ),
        Index("ix_voice_notify_category_configs_guild_id", "guild_id"),
        Index(
            "ix_voice_notify_category_configs_notify_channel_id",
            "notify_channel_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[str] = mapped_column(String, nullable=False)
    category_id: Mapped[str] = mapped_column(String, nullable=False)
    notify_channel_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    @validates("guild_id", "category_id", "notify_channel_id")
    def _validate_ids(self, key: str, value: str) -> str:
        return _validate_discord_id(value, key)

    def __repr__(self) -> str:
        return (
            f"<VoiceNotifyCategoryConfig(id={self.id}, guild_id={self.guild_id}, "
            f"category_id={self.category_id}, "
            f"notify_channel_id={self.notify_channel_id})>"
        )


class VoiceNotifyExclude(Base):
    """カテゴリ通知から除外する VC テーブル。"""

    __tablename__ = "voice_notify_excludes"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "voice_channel_id",
            name="uq_voice_notify_exclude_guild_voice_channel",
        ),
        Index("ix_voice_notify_excludes_guild_id", "guild_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[str] = mapped_column(String, nullable=False)
    voice_channel_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    @validates("guild_id", "voice_channel_id")
    def _validate_ids(self, key: str, value: str) -> str:
        return _validate_discord_id(value, key)

    def __repr__(self) -> str:
        return (
            f"<VoiceNotifyExclude(id={self.id}, guild_id={self.guild_id}, "
            f"voice_channel_id={self.voice_channel_id})>"
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
