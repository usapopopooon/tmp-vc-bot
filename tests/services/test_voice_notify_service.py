"""Tests for voice notification database operations."""

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
    add_voice_notify_exclude,
    delete_voice_notify_by_channel,
    delete_voice_notify_by_guild,
    delete_voice_notify_category_config,
    delete_voice_notify_config,
    delete_voice_notify_exclude,
    get_voice_notify_category_config,
    get_voice_notify_config,
    is_voice_notify_excluded,
    list_voice_notify_category_configs,
    list_voice_notify_configs,
    list_voice_notify_configs_by_voice_channel,
    list_voice_notify_excludes,
    set_voice_notify_category_config,
    set_voice_notify_config,
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


class TestVoiceNotifyConfigOperations:
    """VC 単位の通知設定操作テスト。"""

    async def test_set_get_update_and_list_config(
        self,
        db_session: AsyncSession,
    ) -> None:
        """VC 単位の通知設定を作成・更新・一覧取得できる。"""
        created = await set_voice_notify_config(
            db_session,
            "123",
            "456",
            "789",
        )

        assert created.id is not None
        assert created.guild_id == "123"
        assert created.voice_channel_id == "456"
        assert created.notify_channel_id == "789"

        updated = await set_voice_notify_config(
            db_session,
            "123",
            "456",
            "999",
        )

        assert updated.id == created.id
        assert updated.notify_channel_id == "999"

        found = await get_voice_notify_config(db_session, "123", "456")
        assert found is not None
        assert found.notify_channel_id == "999"

        configs = await list_voice_notify_configs(db_session, "123")
        by_voice = await list_voice_notify_configs_by_voice_channel(
            db_session,
            "123",
            "456",
        )
        assert [config.voice_channel_id for config in configs] == ["456"]
        assert by_voice == configs

    async def test_delete_config(self, db_session: AsyncSession) -> None:
        """VC 単位の通知設定を削除できる。"""
        await set_voice_notify_config(db_session, "123", "456", "789")

        assert await delete_voice_notify_config(db_session, "123", "456") is True
        assert await delete_voice_notify_config(db_session, "123", "456") is False
        assert await get_voice_notify_config(db_session, "123", "456") is None


class TestVoiceNotifyCategoryOperations:
    """カテゴリ単位の通知設定操作テスト。"""

    async def test_set_get_update_and_list_category_config(
        self,
        db_session: AsyncSession,
    ) -> None:
        """カテゴリ単位の通知設定を作成・更新・一覧取得できる。"""
        created = await set_voice_notify_category_config(
            db_session,
            "123",
            "222",
            "789",
        )
        updated = await set_voice_notify_category_config(
            db_session,
            "123",
            "222",
            "999",
        )

        assert updated.id == created.id
        assert updated.notify_channel_id == "999"

        found = await get_voice_notify_category_config(db_session, "123", "222")
        assert found is not None
        assert found.notify_channel_id == "999"

        configs = await list_voice_notify_category_configs(db_session, "123")
        assert [config.category_id for config in configs] == ["222"]

    async def test_delete_category_config(self, db_session: AsyncSession) -> None:
        """カテゴリ単位の通知設定を削除できる。"""
        await set_voice_notify_category_config(db_session, "123", "222", "789")

        assert (
            await delete_voice_notify_category_config(db_session, "123", "222") is True
        )
        assert (
            await delete_voice_notify_category_config(db_session, "123", "222") is False
        )


class TestVoiceNotifyExcludeOperations:
    """カテゴリ通知除外の操作テスト。"""

    async def test_add_list_check_and_delete_exclude(
        self,
        db_session: AsyncSession,
    ) -> None:
        """カテゴリ通知除外を追加・確認・削除できる。"""
        exclude = await add_voice_notify_exclude(db_session, "123", "456")
        duplicate = await add_voice_notify_exclude(db_session, "123", "456")

        assert duplicate.id == exclude.id
        assert await is_voice_notify_excluded(db_session, "123", "456") is True

        excludes = await list_voice_notify_excludes(db_session, "123")
        assert [item.voice_channel_id for item in excludes] == ["456"]

        assert await delete_voice_notify_exclude(db_session, "123", "456") is True
        assert await delete_voice_notify_exclude(db_session, "123", "456") is False
        assert await is_voice_notify_excluded(db_session, "123", "456") is False


class TestVoiceNotifyCleanupOperations:
    """通知設定の一括クリーンアップテスト。"""

    async def test_delete_by_channel_removes_related_settings(
        self,
        db_session: AsyncSession,
    ) -> None:
        """対象/通知先/カテゴリ/除外に一致するチャンネル削除を掃除できる。"""
        await set_voice_notify_config(db_session, "123", "456", "999")
        await set_voice_notify_config(db_session, "123", "111", "456")
        await set_voice_notify_category_config(db_session, "123", "222", "456")
        await add_voice_notify_exclude(db_session, "123", "456")
        await set_voice_notify_config(db_session, "999", "456", "999")

        deleted = await delete_voice_notify_by_channel(db_session, "123", "456")

        assert deleted == 4
        assert await list_voice_notify_configs(db_session, "123") == []
        assert await list_voice_notify_category_configs(db_session, "123") == []
        assert await list_voice_notify_excludes(db_session, "123") == []
        assert len(await list_voice_notify_configs(db_session, "999")) == 1

    async def test_delete_by_guild(
        self,
        db_session: AsyncSession,
    ) -> None:
        """サーバー単位で通知設定を削除できる。"""
        await set_voice_notify_config(db_session, "123", "456", "999")
        await set_voice_notify_category_config(db_session, "234", "222", "999")
        await add_voice_notify_exclude(db_session, "345", "777")

        deleted = await delete_voice_notify_by_guild(db_session, "234")

        assert deleted == 1
        assert await list_voice_notify_category_configs(db_session, "234") == []
        assert len(await list_voice_notify_configs(db_session, "123")) == 1
        assert len(await list_voice_notify_excludes(db_session, "345")) == 1
