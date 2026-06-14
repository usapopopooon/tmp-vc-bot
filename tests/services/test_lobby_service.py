"""Tests for lobby/voice session/voice session member/processed event services."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.constants import DEFAULT_TEST_DATABASE_URL
from src.database.models import Base
from src.services.db_service import (
    add_voice_session_member,
    claim_event,
    cleanup_expired_events,
    create_lobby,
    create_voice_session,
    delete_lobbies_by_guild,
    delete_lobby,
    delete_voice_session,
    delete_voice_sessions_by_guild,
    get_all_lobbies,
    get_all_voice_sessions,
    get_lobbies_by_guild,
    get_lobby_by_channel_id,
    get_voice_session,
    get_voice_session_members_ordered,
    remove_voice_session_member,
    update_voice_session,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    DEFAULT_TEST_DATABASE_URL,
)


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """PostgreSQL テスト DB のセッションを提供する。"""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


class TestLobbyOperations:
    """Tests for lobby database operations."""

    async def test_create_lobby(self, db_session: AsyncSession) -> None:
        """Test creating a lobby."""
        lobby = await create_lobby(
            db_session,
            guild_id="123",
            lobby_channel_id="456",
            category_id="789",
            default_user_limit=10,
        )
        assert lobby.id is not None
        assert lobby.guild_id == "123"
        assert lobby.lobby_channel_id == "456"
        assert lobby.category_id == "789"
        assert lobby.default_user_limit == 10

    async def test_get_lobby_by_channel_id(self, db_session: AsyncSession) -> None:
        """Test getting a lobby by channel ID."""
        await create_lobby(db_session, guild_id="123", lobby_channel_id="456")

        lobby = await get_lobby_by_channel_id(db_session, "456")
        assert lobby is not None
        assert lobby.guild_id == "123"

    async def test_get_lobby_by_channel_id_not_found(
        self, db_session: AsyncSession
    ) -> None:
        """Test getting a non-existent lobby."""
        lobby = await get_lobby_by_channel_id(db_session, "nonexistent")
        assert lobby is None

    async def test_get_lobbies_by_guild(self, db_session: AsyncSession) -> None:
        """Test getting all lobbies for a guild."""
        await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        await create_lobby(db_session, guild_id="123", lobby_channel_id="789")
        await create_lobby(db_session, guild_id="999", lobby_channel_id="111")

        lobbies = await get_lobbies_by_guild(db_session, "123")
        assert len(lobbies) == 2

    async def test_delete_lobby(self, db_session: AsyncSession) -> None:
        """Test deleting a lobby."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")

        result = await delete_lobby(db_session, lobby.id)
        assert result is True

        found = await get_lobby_by_channel_id(db_session, "456")
        assert found is None

    async def test_delete_lobby_not_found(self, db_session: AsyncSession) -> None:
        """Test deleting a non-existent lobby."""
        result = await delete_lobby(db_session, 99999)
        assert result is False


class TestVoiceSessionOperations:
    """Tests for voice session database operations."""

    async def test_create_voice_session(self, db_session: AsyncSession) -> None:
        """Test creating a voice session."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")

        session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        assert session.id is not None
        assert session.channel_id == "789"
        assert session.owner_id == "111"
        assert session.name == "Test Channel"

    async def test_get_voice_session(self, db_session: AsyncSession) -> None:
        """Test getting a voice session."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        found = await get_voice_session(db_session, "789")
        assert found is not None
        assert found.owner_id == "111"

    async def test_get_voice_session_not_found(self, db_session: AsyncSession) -> None:
        """Test getting a non-existent voice session."""
        found = await get_voice_session(db_session, "nonexistent")
        assert found is None

    async def test_get_all_voice_sessions(self, db_session: AsyncSession) -> None:
        """Test getting all voice sessions."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Channel 1",
        )
        await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="790",
            owner_id="222",
            name="Channel 2",
        )

        sessions = await get_all_voice_sessions(db_session)
        assert len(sessions) == 2

    async def test_update_voice_session(self, db_session: AsyncSession) -> None:
        """Test updating a voice session."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        updated = await update_voice_session(
            db_session,
            session,
            name="New Name",
            is_locked=True,
            is_hidden=True,
            owner_id="222",
        )

        assert updated.name == "New Name"
        assert updated.is_locked is True
        assert updated.is_hidden is True
        assert updated.owner_id == "222"

    async def test_update_voice_session_name_only(
        self, db_session: AsyncSession
    ) -> None:
        """Test updating only the name leaves other fields unchanged."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Original",
        )

        updated = await update_voice_session(db_session, session, name="Renamed")

        assert updated.name == "Renamed"
        assert updated.is_locked is False
        assert updated.is_hidden is False
        assert updated.owner_id == "111"

    async def test_update_voice_session_no_params(
        self, db_session: AsyncSession
    ) -> None:
        """Test updating with no parameters changes nothing."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Unchanged",
        )

        updated = await update_voice_session(db_session, session)

        assert updated.name == "Unchanged"
        assert updated.owner_id == "111"
        assert updated.is_locked is False
        assert updated.is_hidden is False

    async def test_delete_voice_session(self, db_session: AsyncSession) -> None:
        """Test deleting a voice session."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        result = await delete_voice_session(db_session, "789")
        assert result is True

        found = await get_voice_session(db_session, "789")
        assert found is None

    async def test_delete_voice_session_not_found(
        self, db_session: AsyncSession
    ) -> None:
        """Test deleting a non-existent voice session."""
        result = await delete_voice_session(db_session, "nonexistent")
        assert result is False

    async def test_voice_session_default_values(self, db_session: AsyncSession) -> None:
        """Test that default values are set correctly."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        assert session.is_locked is False
        assert session.is_hidden is False


class TestVoiceSessionMemberOperations:
    """Tests for voice session member database operations."""

    async def test_add_voice_session_member(self, db_session: AsyncSession) -> None:
        """Test adding a member to a voice session."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        voice_session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        member = await add_voice_session_member(db_session, voice_session.id, "222")

        assert member.id is not None
        assert member.voice_session_id == voice_session.id
        assert member.user_id == "222"
        assert member.joined_at is not None

    async def test_add_voice_session_member_existing(
        self, db_session: AsyncSession
    ) -> None:
        """Test adding an existing member returns the existing record."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        voice_session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        member1 = await add_voice_session_member(db_session, voice_session.id, "222")
        member2 = await add_voice_session_member(db_session, voice_session.id, "222")

        assert member1.id == member2.id
        assert member1.joined_at == member2.joined_at

    async def test_remove_voice_session_member(self, db_session: AsyncSession) -> None:
        """Test removing a member from a voice session."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        voice_session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        await add_voice_session_member(db_session, voice_session.id, "222")

        result = await remove_voice_session_member(db_session, voice_session.id, "222")
        assert result is True

        members = await get_voice_session_members_ordered(db_session, voice_session.id)
        assert len(members) == 0

    async def test_remove_voice_session_member_not_found(
        self, db_session: AsyncSession
    ) -> None:
        """Test removing a non-existent member returns False."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        voice_session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        result = await remove_voice_session_member(
            db_session, voice_session.id, "nonexistent"
        )
        assert result is False

    async def test_get_voice_session_members_ordered(
        self, db_session: AsyncSession
    ) -> None:
        """Test getting members ordered by join time."""
        import asyncio

        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        voice_session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        await add_voice_session_member(db_session, voice_session.id, "first")
        await asyncio.sleep(0.01)
        await add_voice_session_member(db_session, voice_session.id, "second")
        await asyncio.sleep(0.01)
        await add_voice_session_member(db_session, voice_session.id, "third")

        members = await get_voice_session_members_ordered(db_session, voice_session.id)

        assert len(members) == 3
        assert members[0].user_id == "first"
        assert members[1].user_id == "second"
        assert members[2].user_id == "third"

    async def test_get_voice_session_members_ordered_empty(
        self, db_session: AsyncSession
    ) -> None:
        """Test getting members from empty session returns empty list."""
        lobby = await create_lobby(db_session, guild_id="123", lobby_channel_id="456")
        voice_session = await create_voice_session(
            db_session,
            lobby_id=lobby.id,
            channel_id="789",
            owner_id="111",
            name="Test Channel",
        )

        members = await get_voice_session_members_ordered(db_session, voice_session.id)

        assert members == []


class TestDeleteByGuild:
    """Tests for guild-scoped bulk deletion."""

    async def test_delete_lobbies_by_guild(self, db_session: AsyncSession) -> None:
        """指定ギルドのロビーを全て削除できる。"""
        await create_lobby(
            db_session, guild_id="123", lobby_channel_id="200000000000000001"
        )
        await create_lobby(
            db_session, guild_id="123", lobby_channel_id="200000000000000002"
        )
        await create_lobby(
            db_session, guild_id="999", lobby_channel_id="200000000000000003"
        )

        count = await delete_lobbies_by_guild(db_session, "123")
        assert count == 2

        lobbies = await get_lobbies_by_guild(db_session, "123")
        assert len(lobbies) == 0

        other_lobbies = await get_lobbies_by_guild(db_session, "999")
        assert len(other_lobbies) == 1

    async def test_delete_lobbies_by_guild_empty(
        self, db_session: AsyncSession
    ) -> None:
        """存在しないギルドを指定しても 0 が返る。"""
        count = await delete_lobbies_by_guild(db_session, "nonexistent")
        assert count == 0

    async def test_delete_voice_sessions_by_guild(
        self, db_session: AsyncSession
    ) -> None:
        """指定ギルドのボイスセッションを全て削除できる。"""
        lobby1 = await create_lobby(
            db_session, guild_id="123", lobby_channel_id="200000000000000001"
        )
        lobby2 = await create_lobby(
            db_session, guild_id="999", lobby_channel_id="200000000000000002"
        )

        await create_voice_session(
            db_session,
            lobby_id=lobby1.id,
            channel_id="300000000000000001",
            owner_id="400000000000000001",
            name="VC 1",
        )
        await create_voice_session(
            db_session,
            lobby_id=lobby1.id,
            channel_id="300000000000000002",
            owner_id="400000000000000002",
            name="VC 2",
        )
        await create_voice_session(
            db_session,
            lobby_id=lobby2.id,
            channel_id="300000000000000003",
            owner_id="400000000000000003",
            name="VC 3",
        )

        count = await delete_voice_sessions_by_guild(db_session, "123")
        assert count == 2

        all_sessions = await get_all_voice_sessions(db_session)
        target_sessions = [s for s in all_sessions if s.lobby.guild_id == "123"]
        assert len(target_sessions) == 0

        other_sessions = [s for s in all_sessions if s.lobby.guild_id == "999"]
        assert len(other_sessions) == 1

    async def test_delete_voice_sessions_by_guild_empty(
        self, db_session: AsyncSession
    ) -> None:
        """存在しないギルドを指定しても 0 が返る。"""
        count = await delete_voice_sessions_by_guild(db_session, "nonexistent")
        assert count == 0


class TestGetAllLobbies:
    """Tests for get_all_lobbies."""

    async def test_get_all_lobbies(self, db_session: AsyncSession) -> None:
        """全てのロビーを取得できる。"""
        await create_lobby(
            db_session,
            guild_id="100000000000000001",
            lobby_channel_id="200000000000000001",
        )
        await create_lobby(
            db_session,
            guild_id="100000000000000001",
            lobby_channel_id="200000000000000002",
        )
        await create_lobby(
            db_session,
            guild_id="100000000000000002",
            lobby_channel_id="200000000000000003",
        )

        lobbies = await get_all_lobbies(db_session)
        assert len(lobbies) == 3

    async def test_get_all_lobbies_empty(self, db_session: AsyncSession) -> None:
        """ロビーが存在しない場合は空リストを返す。"""
        lobbies = await get_all_lobbies(db_session)
        assert lobbies == []


class TestClaimEvent:
    """claim_event のアトミック重複防止テスト。"""

    async def test_first_claim_succeeds(self, db_session: AsyncSession) -> None:
        """初回 claim は True を返す。"""
        result = await claim_event(db_session, "test:event:1")
        assert result is True

    async def test_duplicate_claim_returns_false(
        self, db_session: AsyncSession
    ) -> None:
        """同一 event_key の2回目の claim は False を返す。"""
        result1 = await claim_event(db_session, "test:event:dup")
        assert result1 is True

        result2 = await claim_event(db_session, "test:event:dup")
        assert result2 is False

    async def test_different_keys_both_succeed(self, db_session: AsyncSession) -> None:
        """異なる event_key なら両方 claim 成功。"""
        r1 = await claim_event(db_session, "test:event:a")
        r2 = await claim_event(db_session, "test:event:b")
        assert r1 is True
        assert r2 is True

    async def test_claim_after_duplicate_still_works(
        self, db_session: AsyncSession
    ) -> None:
        """重複 claim (rollback) 後でも別キーの claim が成功する。"""
        await claim_event(db_session, "test:event:first")
        await claim_event(db_session, "test:event:first")  # False + rollback

        result = await claim_event(db_session, "test:event:second")
        assert result is True


class TestCleanupExpiredEvents:
    """cleanup_expired_events のテスト。"""

    async def test_cleanup_deletes_old_records(self, db_session: AsyncSession) -> None:
        """古いレコードが削除される。"""
        from datetime import UTC, datetime, timedelta

        from src.database.models import ProcessedEvent

        old_event = ProcessedEvent(
            event_key="old:event",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
        db_session.add(old_event)
        await db_session.commit()

        deleted = await cleanup_expired_events(db_session, max_age_seconds=3600)
        assert deleted == 1

    async def test_cleanup_keeps_recent_records(self, db_session: AsyncSession) -> None:
        """新しいレコードは削除されない。"""
        await claim_event(db_session, "recent:event")

        deleted = await cleanup_expired_events(db_session, max_age_seconds=3600)
        assert deleted == 0

    async def test_cleanup_returns_zero_when_empty(
        self, db_session: AsyncSession
    ) -> None:
        """テーブルが空なら 0。"""
        deleted = await cleanup_expired_events(db_session)
        assert deleted == 0
