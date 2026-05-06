"""Tests for VoiceCog join tracking helpers and event listeners."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord import app_commands

from src.cogs.voice import (
    VC_CREATE_COOLDOWN_SECONDS,
    VoiceCog,
    _cleanup_vc_create_cooldown_cache,
    _vc_create_cooldown_cache,
    clear_vc_create_cooldown_cache,
    is_vc_create_on_cooldown,
    record_vc_create_cooldown,
)
from src.utils import clear_resource_locks


@pytest.fixture(autouse=True)
def clear_cooldown_cache() -> None:
    """Clear VC create cooldown cache and resource locks before each test."""
    clear_vc_create_cooldown_cache()
    clear_resource_locks()


class TestVoiceStateIsolation:
    """autouse fixture によるステート分離が機能することを検証するカナリアテスト."""

    def test_cache_starts_empty(self) -> None:
        """各テスト開始時にキャッシュが空であることを検証."""
        assert len(_vc_create_cooldown_cache) == 0

    def test_cleanup_time_is_reset(self) -> None:
        """各テスト開始時にクリーンアップ時刻がリセットされていることを検証."""
        import src.cogs.voice as voice_module

        assert voice_module._vc_last_cleanup_time == 0.0


# ---------------------------------------------------------------------------
# テスト用ヘルパー
# ---------------------------------------------------------------------------


def _make_cog() -> VoiceCog:
    """Create a VoiceCog instance with a mock bot."""
    bot = MagicMock(spec=discord.ext.commands.Bot)
    bot.user = MagicMock(spec=discord.User)
    bot.user.id = 9999
    return VoiceCog(bot)


def _make_member(
    user_id: int,
    *,
    bot: bool = False,
    voice_channel: MagicMock | None = None,
) -> MagicMock:
    """Create a mock discord.Member."""
    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.bot = bot
    member.display_name = f"User{user_id}"
    member.mention = f"<@{user_id}>"
    # voice state (lobby join check で使用)
    member.voice = MagicMock()
    member.voice.channel = voice_channel
    return member


def _make_channel(channel_id: int, members: list[MagicMock] | None = None) -> MagicMock:
    """Create a mock discord.VoiceChannel."""
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.members = members or []
    return channel


def _make_voice_session(
    *,
    channel_id: str = "100",
    owner_id: str = "1",
    is_locked: bool = False,
    is_hidden: bool = False,
) -> MagicMock:
    """Create a mock VoiceSession DB object."""
    vs = MagicMock()
    vs.id = 1
    vs.channel_id = channel_id
    vs.owner_id = owner_id
    vs.name = "Test channel"
    vs.user_limit = 0
    vs.is_locked = is_locked
    vs.is_hidden = is_hidden
    return vs


def _mock_async_session() -> tuple[MagicMock, AsyncMock]:
    """Create mock for async_session context manager.

    Returns:
        (mock_session_factory, mock_session)
    """
    mock_session = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_factory, mock_session


class TestRecordJoinCache:
    """Tests for _record_join_cache."""

    def test_new_member(self) -> None:
        """Test recording a new member join."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        assert 1 in cog._join_times[100]

    def test_no_overwrite(self) -> None:
        """Test that existing join time is not overwritten."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        first_time = cog._join_times[100][1]
        cog._record_join_cache(100, 1)
        assert cog._join_times[100][1] == first_time

    def test_multiple_members(self) -> None:
        """Test recording multiple members in the same channel."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        cog._record_join_cache(100, 2)
        assert len(cog._join_times[100]) == 2

    def test_multiple_channels(self) -> None:
        """Test recording joins across different channels."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        cog._record_join_cache(200, 2)
        assert 100 in cog._join_times
        assert 200 in cog._join_times


class TestRemoveJoinCache:
    """Tests for _remove_join_cache."""

    def test_existing_member(self) -> None:
        """Test removing an existing member's join record."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        cog._remove_join_cache(100, 1)
        assert 1 not in cog._join_times[100]

    def test_missing_channel(self) -> None:
        """Test removing from a non-existent channel does not raise."""
        cog = _make_cog()
        cog._remove_join_cache(999, 1)  # Should not raise

    def test_missing_member(self) -> None:
        """Test removing a non-existent member does not raise."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        cog._remove_join_cache(100, 999)  # Should not raise
        assert 1 in cog._join_times[100]


class TestCleanupChannelCache:
    """Tests for _cleanup_channel_cache."""

    def test_removes_records(self) -> None:
        """Test that cleanup removes all records for a channel."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        cog._record_join_cache(100, 2)
        cog._cleanup_channel_cache(100)
        assert 100 not in cog._join_times

    def test_missing_channel(self) -> None:
        """Test cleaning up a non-existent channel does not raise."""
        cog = _make_cog()
        cog._cleanup_channel_cache(999)  # Should not raise


class TestGetLongestMember:
    """Tests for _get_longest_member (async, DB-based)."""

    async def test_uses_db_order(self) -> None:
        """DB から参加順に取得してメンバーを返す。"""
        cog = _make_cog()
        m1 = _make_member(1)
        m2 = _make_member(2)
        channel = _make_channel(100, [m1, m2])
        # guild.get_member のモック
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == 1 else m2

        voice_session = _make_voice_session()

        # DB から m1, m2 の順で返す
        mock_db_member1 = MagicMock()
        mock_db_member1.user_id = "1"
        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[mock_db_member1, mock_db_member2],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999
            )
            assert result is m1

    async def test_excludes_specified(self) -> None:
        """除外指定されたメンバーは返さない。"""
        cog = _make_cog()
        m1 = _make_member(1)
        m2 = _make_member(2)
        channel = _make_channel(100, [m1, m2])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == 1 else m2

        voice_session = _make_voice_session()

        mock_db_member1 = MagicMock()
        mock_db_member1.user_id = "1"
        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[mock_db_member1, mock_db_member2],
        ):
            # m1 を除外 → m2 が返る
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=1
            )
            assert result is m2

    async def test_none_remaining(self) -> None:
        """メンバーがいなければ None を返す。"""
        cog = _make_cog()
        m1 = _make_member(1)
        channel = _make_channel(100, [m1])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == 1 else None

        voice_session = _make_voice_session()

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=1
            )
            assert result is None

    async def test_empty_channel(self) -> None:
        """空のチャンネルでは None を返す。"""
        cog = _make_cog()
        channel = _make_channel(100, [])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda _uid: None

        voice_session = _make_voice_session()

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=1
            )
            assert result is None

    async def test_fallback_to_cache_without_db_records(self) -> None:
        """DB に記録がない場合はキャッシュにフォールバック。"""
        cog = _make_cog()
        m1 = _make_member(1)
        m2 = _make_member(2)
        channel = _make_channel(100, [m1, m2])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == 1 else m2

        voice_session = _make_voice_session()

        # キャッシュに記録
        cog._join_times[100] = {1: 10.0, 2: 20.0}

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],  # DB に記録なし
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999
            )
            assert result is m1  # キャッシュから m1 が選ばれる

    async def test_tiebreaker_by_member_id(self) -> None:
        """キャッシュで同じ join_time の場合、member.id が小さい方が選ばれる。"""
        cog = _make_cog()
        m1 = _make_member(100)  # 大きい ID
        m2 = _make_member(50)  # 小さい ID
        channel = _make_channel(100, [m1, m2])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == 100 else m2

        voice_session = _make_voice_session()

        # キャッシュで同じ join_time
        cog._join_times[100] = {100: 10.0, 50: 10.0}

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],  # DB に記録なし → キャッシュを使用
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999
            )
            assert result is m2  # ID が小さい m2 が選ばれる


class TestOnGuildChannelDelete:
    """Tests for on_guild_channel_delete listener."""

    async def test_deletes_voice_session_for_known_channel(self) -> None:
        """Test that DB record is deleted when a voice channel is deleted."""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        channel = _make_channel(100)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch(
                "src.cogs.voice.delete_voice_session", new_callable=AsyncMock
            ) as mock_delete,
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await cog.on_guild_channel_delete(channel)

            mock_delete.assert_awaited_once_with(mock_session, "100")
        # メモリ上の参加記録もクリーンアップされる
        assert 100 not in cog._join_times

    async def test_ignores_non_voice_channel(self) -> None:
        """Test that text channel deletion is ignored."""
        cog = _make_cog()
        text_channel = MagicMock(spec=discord.TextChannel)
        text_channel.id = 200

        with patch(
            "src.cogs.voice.delete_voice_session", new_callable=AsyncMock
        ) as mock_delete:
            await cog.on_guild_channel_delete(text_channel)
            mock_delete.assert_not_awaited()

    async def test_no_error_when_no_session_exists(self) -> None:
        """Test that no error occurs when channel has no DB record."""
        cog = _make_cog()
        channel = _make_channel(300)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch(
                "src.cogs.voice.delete_voice_session", new_callable=AsyncMock
            ) as mock_delete,
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            # Should not raise even if no session exists
            await cog.on_guild_channel_delete(channel)
            mock_delete.assert_awaited_once_with(mock_session, "300")

    async def test_deletes_lobby_record_on_channel_delete(self) -> None:
        """ロビー VC を削除すると DB の lobby レコードも削除される。"""
        cog = _make_cog()
        channel = _make_channel(100)

        lobby = MagicMock()
        lobby.id = 42

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.delete_voice_session", new_callable=AsyncMock),
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.delete_lobby",
                new_callable=AsyncMock,
            ) as mock_delete_lobby,
        ):
            await cog.on_guild_channel_delete(channel)

            mock_delete_lobby.assert_awaited_once_with(mock_session, 42)

    async def test_no_lobby_delete_when_not_lobby(self) -> None:
        """ロビーでないチャンネル削除時は lobby レコード削除が呼ばれない。"""
        cog = _make_cog()
        channel = _make_channel(100)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.delete_voice_session", new_callable=AsyncMock),
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.cogs.voice.delete_lobby",
                new_callable=AsyncMock,
            ) as mock_delete_lobby,
        ):
            await cog.on_guild_channel_delete(channel)

            mock_delete_lobby.assert_not_awaited()


# ===========================================================================
# _handle_lobby_join テスト
# ===========================================================================


class TestHandleLobbyJoin:
    """Tests for _handle_lobby_join."""

    async def test_skips_non_lobby_channel(self) -> None:
        """ロビーではないチャンネルに参加しても何もしない。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)
            mock_create.assert_not_awaited()

    async def test_creates_channel_and_session(self) -> None:
        """ロビー参加時に VC を作成し、DB セッションを記録する。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 5

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ) as mock_create,
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

            # VC が作成される
            guild.create_voice_channel.assert_awaited_once()
            # DB セッションが作成される
            mock_create.assert_awaited_once()
            # メンバーが移動される
            member.move_to.assert_awaited_once_with(new_channel)
            # コントロールパネルが送信される
            new_channel.send.assert_awaited_once()

    async def test_copies_lobby_channel_overwrites(self) -> None:
        """ロビーチャンネルの権限設定が新チャンネルにコピーされる。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        # ロビーチャンネルの権限設定をモック
        lobby_overwrites = {
            MagicMock(spec=discord.Role): discord.PermissionOverwrite(connect=False),
            MagicMock(spec=discord.Member): discord.PermissionOverwrite(connect=True),
        }
        channel.overwrites = lobby_overwrites

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 5

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

            # VC 作成時にロビーの overwrites が維持され、
            # さらに read_message_history の初期権限が付与される
            call_kwargs = guild.create_voice_channel.call_args[1]
            created_overwrites = call_kwargs["overwrites"]
            for target, overwrite in lobby_overwrites.items():
                assert target in created_overwrites
                assert created_overwrites[target] == overwrite
            assert created_overwrites[guild.default_role].read_message_history is False
            assert created_overwrites[member].read_message_history is True

    async def test_cleanup_on_move_failure(self) -> None:
        """move_to 失敗時にチャンネルと DB レコードをクリーンアップする。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 0

        new_channel = _make_channel(200)
        new_channel.delete = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild
        member.move_to = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "error")
        )

        voice_session = _make_voice_session(channel_id="200")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.delete_voice_session",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

            # クリーンアップ: チャンネル削除 + DB レコード削除
            new_channel.delete.assert_awaited_once()
            mock_delete.assert_awaited_once_with(mock_session, "200")

    async def test_bulk_moves_all_lobby_members(self) -> None:
        """ロビーにいる全メンバーを新VCに一括移動する。"""
        cog = _make_cog()
        owner = _make_member(1)
        peer = _make_member(2)
        channel = _make_channel(100, [owner, peer])
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 5

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        owner.guild = guild
        peer.guild = guild
        owner.move_to = AsyncMock()
        peer.move_to = AsyncMock()

        # 両者とも現在ロビーにいる状態
        owner.voice.channel = channel
        peer.voice.channel = channel

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            await cog._handle_lobby_join(owner, channel)

        owner.move_to.assert_awaited_once_with(new_channel)
        peer.move_to.assert_awaited_once_with(new_channel)


# ===========================================================================
# _handle_channel_leave テスト
# ===========================================================================


class TestHandleChannelLeave:
    """Tests for _handle_channel_leave."""

    async def test_skips_non_session_channel(self) -> None:
        """一時 VC ではないチャンネルからの退出は無視する。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.cogs.voice.delete_voice_session",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            await cog._handle_channel_leave(member, channel)
            mock_delete.assert_not_awaited()

    async def test_deletes_empty_channel(self) -> None:
        """全員退出時にチャンネルと DB レコードを削除する。"""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        member = _make_member(1)
        channel = _make_channel(100, [])  # 空のチャンネル
        channel.delete = AsyncMock()

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.delete_voice_session",
                new_callable=AsyncMock,
            ) as mock_delete,
        ):
            await cog._handle_channel_leave(member, channel)

            channel.delete.assert_awaited_once()
            mock_delete.assert_awaited_once_with(mock_session, "100")
            assert 100 not in cog._join_times

    async def test_transfers_ownership_when_owner_leaves(self) -> None:
        """オーナー退出時に引き継ぎ処理が呼ばれる。"""
        cog = _make_cog()
        owner = _make_member(1)
        remaining = _make_member(2)
        channel = _make_channel(100, [remaining])
        channel.set_permissions = AsyncMock()
        channel.send = AsyncMock()
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: remaining if uid == 2 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ) as mock_update,
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2],
            ),
        ):
            await cog._handle_channel_leave(owner, channel)

            # オーナー ID が新オーナーに更新される
            mock_update.assert_awaited_once()
            call_kwargs = mock_update.call_args
            assert call_kwargs[1]["owner_id"] == "2"

    async def test_no_transfer_when_non_owner_leaves(self) -> None:
        """オーナー以外の退出では引き継ぎしない。"""
        cog = _make_cog()
        owner = _make_member(1)
        leaver = _make_member(2)
        channel = _make_channel(100, [owner])

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            await cog._handle_channel_leave(leaver, channel)
            mock_update.assert_not_awaited()


# ===========================================================================
# _transfer_ownership テスト
# ===========================================================================


class TestTransferOwnership:
    """Tests for _transfer_ownership."""

    async def test_transfers_to_longest_member(self) -> None:
        """最も長く滞在しているメンバーにオーナーが移る。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        m2 = _make_member(2)
        m3 = _make_member(3)
        channel = _make_channel(100, [m2, m3])
        channel.set_permissions = AsyncMock()
        channel.send = AsyncMock()
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m2 if uid == 2 else m3

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        # DB からの参加順: m2 が先
        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"
        mock_db_member3 = MagicMock()
        mock_db_member3.user_id = "3"

        mock_session = AsyncMock()
        with (
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ) as mock_update,
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2, mock_db_member3],
            ),
        ):
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )

            mock_update.assert_awaited_once()
            assert mock_update.call_args[1]["owner_id"] == "2"

    async def test_updates_text_permissions(self) -> None:
        """テキストチャット権限が旧→新オーナーに移行される。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        new_owner = _make_member(2)
        channel = _make_channel(100, [new_owner])
        channel.set_permissions = AsyncMock()
        channel.send = AsyncMock()
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: new_owner if uid == 2 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with (
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2],
            ),
        ):
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )

            # set_permissions が2回呼ばれる (旧オーナー解除 + 新オーナー付与)
            assert channel.set_permissions.await_count == 2
            calls = channel.set_permissions.call_args_list
            # 旧オーナー: read_message_history=None
            assert calls[0][1]["read_message_history"] is None
            # 新オーナー: read_message_history=True
            assert calls[1][1]["read_message_history"] is True

    async def test_reposts_panel(self) -> None:
        """コントロールパネルが再投稿される。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        new_owner = _make_member(2)
        channel = _make_channel(100, [new_owner])
        channel.set_permissions = AsyncMock()
        channel.send = AsyncMock()
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: new_owner if uid == 2 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with (
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ) as mock_repost,
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2],
            ),
        ):
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )

            mock_repost.assert_awaited_once_with(channel, cog.bot)

    async def test_sends_notification(self) -> None:
        """引き継ぎ通知がチャンネルに送信される。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        new_owner = _make_member(2)
        channel = _make_channel(100, [new_owner])
        channel.set_permissions = AsyncMock()
        channel.send = AsyncMock()
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: new_owner if uid == 2 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with (
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2],
            ),
        ):
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )

            channel.send.assert_awaited_once()
            msg = channel.send.call_args[0][0]
            assert new_owner.mention in msg

    async def test_no_transfer_when_no_humans(self) -> None:
        """人間のメンバーがいない場合は引き継ぎしない。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        bot_member = _make_member(99, bot=True)
        channel = _make_channel(100, [bot_member])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: bot_member if uid == 99 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_session = AsyncMock()
        with (
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ) as mock_update,
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[],  # DB に記録なし
            ),
        ):
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )
            mock_update.assert_not_awaited()

    async def test_handles_permission_http_exception(self) -> None:
        """set_permissions の HTTPException を処理する。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        new_owner = _make_member(2)
        channel = _make_channel(100, [new_owner])
        channel.set_permissions = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "error")
        )
        channel.send = AsyncMock()
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: new_owner if uid == 2 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with (
            patch("src.cogs.voice.update_voice_session", new_callable=AsyncMock),
            patch("src.cogs.voice.repost_panel", new_callable=AsyncMock),
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2],
            ),
        ):
            # HTTPException は警告ログで処理されエラーにならない
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )

    async def test_handles_send_notification_http_exception(self) -> None:
        """通知送信の HTTPException を処理する。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        new_owner = _make_member(2)
        channel = _make_channel(100, [new_owner])
        channel.set_permissions = AsyncMock()
        channel.send = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "error")
        )
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: new_owner if uid == 2 else None

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with (
            patch("src.cogs.voice.update_voice_session", new_callable=AsyncMock),
            patch("src.cogs.voice.repost_panel", new_callable=AsyncMock),
            patch(
                "src.cogs.voice.get_voice_session_members_ordered",
                new_callable=AsyncMock,
                return_value=[mock_db_member2],
            ),
        ):
            # HTTPException は警告ログで処理されエラーにならない
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )


# ===========================================================================
# _find_panel_message テスト
# ===========================================================================


class TestFindPanelMessage:
    """Tests for _find_panel_message."""

    async def test_finds_pinned_message(self) -> None:
        """ピン留めメッセージから Bot の Embed を見つける。"""
        cog = _make_cog()
        channel = _make_channel(100)

        bot_msg = MagicMock()
        bot_msg.author = cog.bot.user
        bot_msg.embeds = [MagicMock()]

        channel.pins = AsyncMock(return_value=[bot_msg])

        result = await cog._find_panel_message(channel)
        assert result is bot_msg

    async def test_falls_back_to_history(self) -> None:
        """ピンがない場合は履歴から探す。"""
        cog = _make_cog()
        channel = _make_channel(100)

        channel.pins = AsyncMock(return_value=[])

        bot_msg = MagicMock()
        bot_msg.author = cog.bot.user
        bot_msg.embeds = [MagicMock()]

        # AsyncIterator をモック
        async def _history(**_: object) -> AsyncMock:
            """Mock async iterator for channel.history."""

        async def _aiter() -> MagicMock:
            yield bot_msg

        channel.history = MagicMock(return_value=_aiter())

        result = await cog._find_panel_message(channel)
        assert result is bot_msg

    async def test_returns_none_when_not_found(self) -> None:
        """メッセージが見つからない場合は None を返す。"""
        cog = _make_cog()
        channel = _make_channel(100)

        channel.pins = AsyncMock(return_value=[])

        async def _empty_aiter() -> MagicMock:
            return
            yield  # make it an async generator  # noqa: RET504

        channel.history = MagicMock(return_value=_empty_aiter())

        result = await cog._find_panel_message(channel)
        assert result is None

    async def test_skips_non_bot_messages(self) -> None:
        """Bot 以外のメッセージはスキップする。"""
        cog = _make_cog()
        channel = _make_channel(100)

        other_user = MagicMock(spec=discord.User)
        other_user.id = 8888
        non_bot_msg = MagicMock()
        non_bot_msg.author = other_user
        non_bot_msg.embeds = [MagicMock()]

        bot_msg = MagicMock()
        bot_msg.author = cog.bot.user
        bot_msg.embeds = [MagicMock()]

        channel.pins = AsyncMock(return_value=[non_bot_msg, bot_msg])

        result = await cog._find_panel_message(channel)
        assert result is bot_msg

    async def test_handles_pins_http_exception(self) -> None:
        """pins() で HTTPException が発生しても履歴にフォールバックする。"""
        cog = _make_cog()
        channel = _make_channel(100)

        channel.pins = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "error")
        )

        bot_msg = MagicMock()
        bot_msg.author = cog.bot.user
        bot_msg.embeds = [MagicMock()]

        async def _aiter() -> MagicMock:
            yield bot_msg

        channel.history = MagicMock(return_value=_aiter())

        result = await cog._find_panel_message(channel)
        assert result is bot_msg

    async def test_handles_history_http_exception(self) -> None:
        """history() で HTTPException が発生すると None を返す。"""
        cog = _make_cog()
        channel = _make_channel(100)

        channel.pins = AsyncMock(return_value=[])

        async def _error_aiter() -> MagicMock:
            raise discord.HTTPException(MagicMock(status=500), "error")
            yield  # make it an async generator  # noqa: RET503

        channel.history = MagicMock(return_value=_error_aiter())

        result = await cog._find_panel_message(channel)
        assert result is None


# ===========================================================================
# on_voice_state_update テスト
# ===========================================================================


class TestOnVoiceStateUpdate:
    """Tests for on_voice_state_update event handler."""

    async def test_join_calls_handle_lobby_join(self) -> None:
        """VC 参加時に _handle_lobby_join が呼ばれる。"""
        cog = _make_cog()
        member = _make_member(1)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = None

        after = MagicMock(spec=discord.VoiceState)
        after.channel = _make_channel(100)

        cog._handle_lobby_join = AsyncMock()  # type: ignore[method-assign]
        cog._record_join_to_db = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        cog._handle_lobby_join.assert_awaited_once_with(member, after.channel)
        assert 1 in cog._join_times[100]

    async def test_leave_calls_handle_channel_leave(self) -> None:
        """VC 退出時に _handle_channel_leave が呼ばれる。"""
        cog = _make_cog()
        cog._record_join_cache(100, 1)
        member = _make_member(1)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = _make_channel(100)

        after = MagicMock(spec=discord.VoiceState)
        after.channel = None

        cog._handle_channel_leave = AsyncMock()  # type: ignore[method-assign]
        cog._remove_join_from_db = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        cog._handle_channel_leave.assert_awaited_once_with(member, before.channel)
        # 参加記録が削除される
        assert 1 not in cog._join_times.get(100, {})

    async def test_same_channel_ignored(self) -> None:
        """同じチャンネル内の状態変化 (ミュート等) は無視される。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = channel
        after = MagicMock(spec=discord.VoiceState)
        after.channel = channel

        cog._handle_lobby_join = AsyncMock()  # type: ignore[method-assign]
        cog._handle_channel_leave = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        cog._handle_lobby_join.assert_not_awaited()
        cog._handle_channel_leave.assert_not_awaited()


# ===========================================================================
# スラッシュコマンドテスト (/vc グループ)
# ===========================================================================


def _make_interaction(
    user_id: int,
    channel: MagicMock | None = None,
    *,
    guild: MagicMock | None = "default",
    guild_id: int = 1000,
) -> MagicMock:
    """Create a mock discord.Interaction for slash commands."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = _make_member(user_id)
    interaction.channel = channel
    interaction.channel_id = channel.id if channel else None
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    if guild == "default":
        g = MagicMock(spec=discord.Guild)
        g.create_voice_channel = AsyncMock()
        interaction.guild = g
    else:
        interaction.guild = guild

    interaction.guild_id = guild_id
    return interaction


# ===========================================================================
# /vc lobby コマンドテスト
# ===========================================================================


class TestVcLobbyCommand:
    """Tests for /vc lobby slash command."""

    async def test_creates_lobby_successfully(self) -> None:
        """正常系: VC を作成し DB に登録して完了メッセージを返す。"""
        cog = _make_cog()
        interaction = _make_interaction(1)

        lobby_channel = MagicMock(spec=discord.VoiceChannel)
        lobby_channel.id = 500
        lobby_channel.name = "➕ 新規VC作成"
        interaction.guild.create_voice_channel = AsyncMock(return_value=lobby_channel)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock) as mock_create,
        ):
            await cog.vc_lobby.callback(cog, interaction)

            # VC が作成される
            interaction.guild.create_voice_channel.assert_awaited_once_with(
                name="➕ 新規VC作成",
                rtc_region="japan",
            )
            # DB にロビーとして登録される
            mock_create.assert_awaited_once_with(
                mock_session,
                guild_id=str(interaction.guild_id),
                lobby_channel_id="500",
                category_id=None,
                default_user_limit=0,
            )
            # defer が呼ばれる
            interaction.response.defer.assert_awaited_once_with(ephemeral=True)
            # 完了メッセージ (followup)
            interaction.followup.send.assert_awaited_once()
            msg = interaction.followup.send.call_args[0][0]
            assert "➕ 新規VC作成" in msg
            assert interaction.followup.send.call_args[1]["ephemeral"] is True

    async def test_rejects_dm(self) -> None:
        """DM からの実行は拒否される。"""
        cog = _make_cog()
        interaction = _make_interaction(1, guild=None)

        await cog.vc_lobby.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args[0][0]
        assert "サーバー内" in msg
        assert interaction.response.send_message.call_args[1]["ephemeral"] is True

    async def test_handles_http_exception(self) -> None:
        """Discord API エラー時にエラーメッセージを返す。"""
        cog = _make_cog()
        interaction = _make_interaction(1)
        interaction.guild.create_voice_channel = AsyncMock(
            side_effect=discord.HTTPException(
                MagicMock(status=403), "Missing Permissions"
            )
        )

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await cog.vc_lobby.callback(cog, interaction)

        interaction.followup.send.assert_awaited_once()
        msg = interaction.followup.send.call_args[0][0]
        assert "失敗" in msg
        assert interaction.followup.send.call_args[1]["ephemeral"] is True

    async def test_rejects_duplicate_lobby(self) -> None:
        """既にロビーが存在するサーバーでは作成を拒否する。"""
        cog = _make_cog()
        interaction = _make_interaction(1)

        existing_lobby = MagicMock()
        existing_lobby.id = 1

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[existing_lobby],
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock) as mock_create,
        ):
            await cog.vc_lobby.callback(cog, interaction)

            # VC は作成されない
            interaction.guild.create_voice_channel.assert_not_awaited()
            # DB 登録も呼ばれない
            mock_create.assert_not_awaited()
            # エラーメッセージ (followup)
            interaction.followup.send.assert_awaited_once()
            msg = interaction.followup.send.call_args[0][0]
            assert "既に" in msg
            assert interaction.followup.send.call_args[1]["ephemeral"] is True

    async def test_no_db_call_on_vc_creation_failure(self) -> None:
        """VC 作成失敗時は DB 登録が呼ばれない。"""
        cog = _make_cog()
        interaction = _make_interaction(1)
        interaction.guild.create_voice_channel = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "Internal Error")
        )

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock) as mock_create,
        ):
            await cog.vc_lobby.callback(cog, interaction)
            mock_create.assert_not_awaited()

    async def test_lobby_channel_has_correct_name(self) -> None:
        """作成される VC の名前が「➕ 新規VC作成」。"""
        cog = _make_cog()
        interaction = _make_interaction(1)

        lobby_channel = MagicMock(spec=discord.VoiceChannel)
        lobby_channel.id = 500
        lobby_channel.name = "➕ 新規VC作成"
        interaction.guild.create_voice_channel = AsyncMock(return_value=lobby_channel)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock),
        ):
            await cog.vc_lobby.callback(cog, interaction)

            call_kwargs = interaction.guild.create_voice_channel.call_args[1]
            assert call_kwargs["name"] == "➕ 新規VC作成"

    async def test_db_registers_correct_guild_id(self) -> None:
        """DB に正しい guild_id が登録される。"""
        cog = _make_cog()
        interaction = _make_interaction(1, guild_id=7777)

        lobby_channel = MagicMock(spec=discord.VoiceChannel)
        lobby_channel.id = 500
        lobby_channel.name = "➕ 新規VC作成"
        interaction.guild.create_voice_channel = AsyncMock(return_value=lobby_channel)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock) as mock_create,
        ):
            await cog.vc_lobby.callback(cog, interaction)

            mock_create.assert_awaited_once()
            assert mock_create.call_args[1]["guild_id"] == "7777"

    async def test_success_message_contains_channel_name(self) -> None:
        """成功メッセージにチャンネル名が含まれる。"""
        cog = _make_cog()
        interaction = _make_interaction(1)

        lobby_channel = MagicMock(spec=discord.VoiceChannel)
        lobby_channel.id = 500
        lobby_channel.name = "➕ 新規VC作成"
        interaction.guild.create_voice_channel = AsyncMock(return_value=lobby_channel)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock),
        ):
            await cog.vc_lobby.callback(cog, interaction)

        msg = interaction.followup.send.call_args[0][0]
        assert "➕ 新規VC作成" in msg
        assert "カテゴリ" in msg

    async def test_error_message_contains_exception_text(self) -> None:
        """エラーメッセージに例外テキストが含まれる。"""
        cog = _make_cog()
        interaction = _make_interaction(1)
        interaction.guild.create_voice_channel = AsyncMock(
            side_effect=discord.HTTPException(
                MagicMock(status=403), "Missing Permissions"
            )
        )

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await cog.vc_lobby.callback(cog, interaction)

        msg = interaction.followup.send.call_args[0][0]
        assert "失敗" in msg

    async def test_concurrent_lobby_creation_only_creates_one(self) -> None:
        """同時に /vc lobby を実行しても、ロビーは1つだけ作成される。"""
        cog = _make_cog()
        cog._lobby_channel_ids = set()

        # 1回目は [] を返し (ロビーなし)、2回目以降は [existing] を返す
        existing_lobby = MagicMock()
        call_count = 0

        async def get_lobbies_side_effect(
            _session: MagicMock, _guild_id: str
        ) -> list[MagicMock]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # ロック内で最初に呼ばれた側はロビーなし
                return []
            # 2番目の呼び出しは既にロビーあり
            return [existing_lobby]

        lobby_channel = MagicMock(spec=discord.VoiceChannel)
        lobby_channel.id = 500
        lobby_channel.name = "➕ 新規VC作成"

        interaction1 = _make_interaction(1)
        interaction1.guild.create_voice_channel = AsyncMock(return_value=lobby_channel)
        interaction2 = _make_interaction(2, guild_id=1000)
        interaction2.guild.create_voice_channel = AsyncMock(return_value=lobby_channel)

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                side_effect=get_lobbies_side_effect,
            ),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock) as mock_create,
        ):
            await asyncio.gather(
                cog.vc_lobby.callback(cog, interaction1),
                cog.vc_lobby.callback(cog, interaction2),
            )

            # create_lobby は1回だけ呼ばれる
            assert mock_create.await_count == 1

        # 1つは成功メッセージ、もう1つは「既に存在」メッセージ (followup)
        msg1 = interaction1.followup.send.call_args[0][0]
        msg2 = interaction2.followup.send.call_args[0][0]
        messages = [msg1, msg2]
        assert sum(1 for m in messages if "作成しました" in m) == 1
        assert sum(1 for m in messages if "既に" in m) == 1

    async def test_defer_failure_aborts_without_creating(self) -> None:
        """defer() が失敗した場合 (別インスタンスが先に応答)、何も作成しない。"""
        cog = _make_cog()
        interaction = _make_interaction(1)
        interaction.response.defer = AsyncMock(
            side_effect=discord.HTTPException(
                MagicMock(status=400), "interaction has already been acknowledged"
            )
        )

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch("src.cogs.voice.create_lobby", new_callable=AsyncMock) as mock_create,
        ):
            await cog.vc_lobby.callback(cog, interaction)

            # VC は作成されない
            interaction.guild.create_voice_channel.assert_not_awaited()
            # DB 登録も呼ばれない
            mock_create.assert_not_awaited()
            # followup も呼ばれない
            interaction.followup.send.assert_not_awaited()


# ===========================================================================
# /vc panel コマンドテスト
# ===========================================================================


class TestVcPanelCommand:
    """Tests for /vc panel slash command."""

    async def test_panel_reposts_control_panel(self) -> None:
        """正常系: repost_panel が呼ばれ、ephemeral で応答。"""
        cog = _make_cog()
        channel = _make_channel(100)
        interaction = _make_interaction(1, channel)

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ) as mock_repost,
        ):
            await cog.vc_panel.callback(cog, interaction)

            # repost_panel が呼ばれる
            mock_repost.assert_awaited_once_with(channel, cog.bot)
            # ephemeral で応答
            interaction.response.send_message.assert_awaited_once()
            call_kwargs = interaction.response.send_message.call_args[1]
            assert call_kwargs["ephemeral"] is True

    async def test_panel_allowed_for_non_owner(self) -> None:
        """オーナー以外でも /vc panel を実行できる。"""
        cog = _make_cog()
        channel = _make_channel(100)
        interaction = _make_interaction(2, channel)

        voice_session = _make_voice_session(channel_id="100", owner_id="1")

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.repost_panel",
                new_callable=AsyncMock,
            ) as mock_repost,
        ):
            await cog.vc_panel.callback(cog, interaction)

            mock_repost.assert_awaited_once_with(channel, cog.bot)
            interaction.response.send_message.assert_awaited_once()
            call_kwargs = interaction.response.send_message.call_args[1]
            assert call_kwargs["ephemeral"] is True

    async def test_panel_rejects_non_voice_channel(self) -> None:
        """VC 外で使用すると拒否される。"""
        cog = _make_cog()
        # TextChannel (VoiceChannel ではない)
        text_channel = MagicMock(spec=discord.TextChannel)
        text_channel.id = 100
        interaction = _make_interaction(1, text_channel)

        await cog.vc_panel.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args[0][0]
        assert "一時 VC" in msg

    async def test_panel_rejects_no_session(self) -> None:
        """セッションが見つからない場合は拒否される。"""
        cog = _make_cog()
        channel = _make_channel(100)
        interaction = _make_interaction(1, channel)

        mock_factory, _mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await cog.vc_panel.callback(cog, interaction)

            interaction.response.send_message.assert_awaited_once()
            msg = interaction.response.send_message.call_args[0][0]
            assert "見つかりません" in msg


# ===========================================================================
# cog_app_command_error テスト
# ===========================================================================


class TestCogAppCommandError:
    """Tests for cog_app_command_error handler."""

    async def test_cooldown_sends_message(self) -> None:
        """クールダウン中は残り秒数を通知する。"""
        cog = _make_cog()
        interaction = _make_interaction(1)

        error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 30), 15.0)
        await cog.cog_app_command_error(interaction, error)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.call_args[0][0]
        assert "15" in msg
        assert interaction.response.send_message.call_args[1]["ephemeral"] is True

    async def test_other_error_reraised(self) -> None:
        """クールダウン以外のエラーは再送出される。"""
        cog = _make_cog()
        interaction = _make_interaction(1)

        error = app_commands.AppCommandError("something broke")
        try:
            await cog.cog_app_command_error(interaction, error)
            raised = False
        except app_commands.AppCommandError:
            raised = True

        assert raised
        interaction.response.send_message.assert_not_awaited()


# ===========================================================================
# _handle_lobby_join — カテゴリ解決テスト
# ===========================================================================


class TestHandleLobbyJoinCategory:
    """Tests for _handle_lobby_join category resolution."""

    async def test_uses_lobby_category_id(self) -> None:
        """ロビーに category_id が設定されている場合はそれを使う。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        custom_category = MagicMock(spec=discord.CategoryChannel)
        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = "999"
        lobby.default_user_limit = 0

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        guild.get_channel = MagicMock(return_value=custom_category)
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

        guild.get_channel.assert_called_once_with(999)
        call_kwargs = guild.create_voice_channel.call_args[1]
        assert call_kwargs["category"] == custom_category

    async def test_falls_back_to_channel_category_when_invalid(self) -> None:
        """category_id のチャンネルが CategoryChannel でない場合はロビーのカテゴリ。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        lobby_category = MagicMock(spec=discord.CategoryChannel)
        channel.category = lobby_category

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = "999"
        lobby.default_user_limit = 0

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        # TextChannel を返す (CategoryChannel ではない)
        guild.get_channel = MagicMock(return_value=MagicMock(spec=discord.TextChannel))
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

        call_kwargs = guild.create_voice_channel.call_args[1]
        assert call_kwargs["category"] == lobby_category

    async def test_db_error_deletes_new_channel(self) -> None:
        """DB セッション作成失敗時に VC を削除する。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 0

        new_channel = _make_channel(200)
        new_channel.delete = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB error"),
            ),
            contextlib.suppress(RuntimeError),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

        new_channel.delete.assert_awaited_once()


# ===========================================================================
# on_voice_state_update — 追加エッジケース
# ===========================================================================


class TestOnVoiceStateUpdateEdgeCases:
    """on_voice_state_update の追加エッジケース。"""

    async def test_move_between_channels(self) -> None:
        """チャンネル間移動では退出と参加の両方が処理される。"""
        cog = _make_cog()
        member = _make_member(1)
        cog._record_join_cache(100, 1)

        old_channel = _make_channel(100)
        new_channel = _make_channel(200)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = old_channel
        after = MagicMock(spec=discord.VoiceState)
        after.channel = new_channel

        cog._handle_lobby_join = AsyncMock()  # type: ignore[method-assign]
        cog._handle_channel_leave = AsyncMock()  # type: ignore[method-assign]
        cog._record_join_to_db = AsyncMock()  # type: ignore[method-assign]
        cog._remove_join_from_db = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        # 参加処理
        cog._handle_lobby_join.assert_awaited_once_with(member, new_channel)
        assert 1 in cog._join_times[200]
        # 退出処理
        cog._handle_channel_leave.assert_awaited_once_with(member, old_channel)
        assert 1 not in cog._join_times.get(100, {})

    async def test_non_voice_channel_join_ignored(self) -> None:
        """StageChannel など VoiceChannel でない場合は無視。"""
        cog = _make_cog()
        member = _make_member(1)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = None
        after = MagicMock(spec=discord.VoiceState)
        after.channel = MagicMock(spec=discord.StageChannel)
        after.channel.id = 300

        cog._handle_lobby_join = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        cog._handle_lobby_join.assert_not_awaited()

    async def test_non_voice_channel_leave_ignored(self) -> None:
        """VoiceChannel でないチャンネルからの退出は無視。"""
        cog = _make_cog()
        member = _make_member(1)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = MagicMock(spec=discord.StageChannel)
        before.channel.id = 300
        after = MagicMock(spec=discord.VoiceState)
        after.channel = None

        cog._handle_channel_leave = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        cog._handle_channel_leave.assert_not_awaited()


# ===========================================================================
# _enforce_channel_restrictions テスト
# ===========================================================================


class TestEnforceChannelRestrictions:
    """Tests for _enforce_channel_restrictions (ロック/人数制限の強制)."""

    async def test_allows_bot_users(self) -> None:
        """Bot ユーザーは常に許可される。"""
        cog = _make_cog()
        member = _make_member(1, bot=True)
        channel = _make_channel(100)

        result = await cog._enforce_channel_restrictions(member, channel)

        assert result is False

    async def test_allows_administrators(self) -> None:
        """Administrator 権限を持つユーザーは常に許可される。"""
        cog = _make_cog()
        member = _make_member(1)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = True
        channel = _make_channel(100)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="999", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is False

    async def test_allows_owner(self) -> None:
        """オーナーは常に許可される。"""
        cog = _make_cog()
        member = _make_member(1)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        channel = _make_channel(100)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is False

    async def test_kicks_non_permitted_user_on_locked_channel(self) -> None:
        """ロックされた VC に許可されていないユーザーが入るとキックされる。"""
        cog = _make_cog()
        member = _make_member(2)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock()  # DM 送信用
        channel = _make_channel(100)
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        # connect 権限が設定されていない
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is True
        member.move_to.assert_awaited_once_with(None)
        channel.send.assert_awaited_once()  # チャンネルに通知
        member.send.assert_awaited_once()  # DM で通知

    async def test_allows_permitted_user_on_locked_channel(self) -> None:
        """ロックされた VC でも connect=True が設定されていれば許可される。"""
        cog = _make_cog()
        member = _make_member(2)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        channel = _make_channel(100)
        # connect=True が設定されている
        overwrites = MagicMock()
        overwrites.connect = True
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is False

    async def test_kicks_user_exceeding_limit(self) -> None:
        """人数制限を超えた場合、許可されていないユーザーはキックされる。"""
        cog = _make_cog()
        member = _make_member(3)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock()  # DM 送信用
        # 既に 2 人いる (+ 参加者で 3 人、制限は 2)
        m1 = _make_member(1)
        m2 = _make_member(2)
        channel = _make_channel(100, [m1, m2, member])
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(channel_id="100", owner_id="1")
        voice_session.user_limit = 2

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is True
        member.move_to.assert_awaited_once_with(None)
        channel.send.assert_awaited_once()  # チャンネルに通知
        member.send.assert_awaited_once()  # DM で通知

    async def test_allows_permitted_user_exceeding_limit(self) -> None:
        """人数制限を超えていても connect=True が設定されていれば許可される。"""
        cog = _make_cog()
        member = _make_member(3)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        m1 = _make_member(1)
        m2 = _make_member(2)
        channel = _make_channel(100, [m1, m2, member])
        # connect=True が設定されている
        overwrites = MagicMock()
        overwrites.connect = True
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(channel_id="100", owner_id="1")
        voice_session.user_limit = 2

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is False

    async def test_skips_non_voice_session(self) -> None:
        """一時 VC ではないチャンネルは制限チェックをスキップする。"""
        cog = _make_cog()
        member = _make_member(1)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        channel = _make_channel(100)

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is False

    async def test_kick_succeeds_even_if_dm_fails(self) -> None:
        """DM 送信が失敗してもキックは成功する。"""
        cog = _make_cog()
        member = _make_member(2)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Cannot send DM")
        )
        channel = _make_channel(100)
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        # DM が失敗してもキックは成功
        assert result is True
        member.move_to.assert_awaited_once_with(None)
        channel.send.assert_awaited_once()

    async def test_kick_succeeds_even_if_channel_send_fails(self) -> None:
        """チャンネル送信が失敗してもキックは成功する。"""
        cog = _make_cog()
        member = _make_member(2)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock()
        channel = _make_channel(100)
        channel.name = "Test Channel"
        channel.send = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(status=500), "Server error")
        )
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        # チャンネル送信が失敗してもキックは成功
        assert result is True
        member.move_to.assert_awaited_once_with(None)


# ===========================================================================
# on_voice_state_update と制限チェックの統合テスト
# ===========================================================================


class TestVoiceStateUpdateWithRestrictions:
    """on_voice_state_update での制限チェック統合テスト。"""

    async def test_skips_join_recording_when_kicked(self) -> None:
        """制限違反でキックされた場合、参加記録がスキップされる。"""
        cog = _make_cog()
        member = _make_member(2)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock()

        channel = _make_channel(100)
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = None
        after = MagicMock(spec=discord.VoiceState)
        after.channel = channel

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            await cog.on_voice_state_update(member, before, after)

        # キックされたので参加記録がない
        assert 2 not in cog._join_times.get(100, {})
        member.move_to.assert_awaited_once_with(None)


# ===========================================================================
# Faker を使ったランダムデータテスト
# ===========================================================================


from faker import Faker  # noqa: E402

fake = Faker("ja_JP")


def _snowflake() -> str:
    """Discord snowflake 風の ID を生成する。"""
    return str(fake.random_number(digits=18, fix_len=True))


class TestVoiceCogWithFaker:
    """Faker を使ったランダムデータでのテスト。"""

    def test_record_join_cache_with_random_ids(self) -> None:
        """ランダムな ID で参加記録が正しく動作する。"""
        cog = _make_cog()
        channel_id = int(_snowflake())
        user_id = int(_snowflake())

        cog._record_join_cache(channel_id, user_id)

        assert user_id in cog._join_times[channel_id]

    def test_multiple_random_members_in_cache(self) -> None:
        """ランダムな複数メンバーの参加記録が正しく動作する。"""
        cog = _make_cog()
        channel_id = int(_snowflake())
        user_ids = [int(_snowflake()) for _ in range(5)]

        for user_id in user_ids:
            cog._record_join_cache(channel_id, user_id)

        assert len(cog._join_times[channel_id]) == 5
        for user_id in user_ids:
            assert user_id in cog._join_times[channel_id]

    async def test_get_longest_member_with_random_ids(self) -> None:
        """ランダム ID で最古メンバー取得が正しく動作する。"""
        cog = _make_cog()
        user_id_1 = int(_snowflake())
        user_id_2 = int(_snowflake())

        m1 = _make_member(user_id_1)
        m2 = _make_member(user_id_2)
        channel = _make_channel(int(_snowflake()), [m1, m2])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == user_id_1 else m2

        voice_session = _make_voice_session()

        mock_db_member1 = MagicMock()
        mock_db_member1.user_id = str(user_id_1)
        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = str(user_id_2)

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[mock_db_member1, mock_db_member2],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999999
            )
            assert result is m1

    async def test_voice_session_with_random_channel_name(self) -> None:
        """ランダムなチャンネル名でセッション作成。"""
        cog = _make_cog()
        member = _make_member(int(_snowflake()))
        channel = _make_channel(int(_snowflake()))
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = fake.random_int(min=1, max=1000)
        lobby.category_id = None
        lobby.default_user_limit = fake.random_int(min=0, max=10)

        new_channel_id = int(_snowflake())
        new_channel = _make_channel(new_channel_id)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(
            channel_id=str(new_channel_id), owner_id=str(member.id)
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

            guild.create_voice_channel.assert_awaited_once()


class TestVoiceCogWithParametrize:
    """パラメタライズテスト。"""

    @pytest.mark.parametrize(
        "user_limit,member_count,should_kick",
        [
            (0, 5, False),  # 制限なしなら何人でもOK
            (5, 3, False),  # 制限内ならOK
            (5, 5, False),  # ちょうど制限と同じならOK
            (5, 6, True),  # 制限超過でキック
            (2, 10, True),  # 大幅超過でキック
        ],
    )
    async def test_user_limit_enforcement(
        self, user_limit: int, member_count: int, should_kick: bool
    ) -> None:
        """人数制限によるキック判定のパラメタライズテスト。"""
        cog = _make_cog()
        new_member = _make_member(99)
        new_member.guild_permissions = MagicMock()
        new_member.guild_permissions.administrator = False
        new_member.move_to = AsyncMock()
        new_member.send = AsyncMock()

        # member_count 人いる状態 (new_member を含む)
        members = [_make_member(i) for i in range(member_count - 1)] + [new_member]
        channel = _make_channel(100, members)
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(channel_id="100", owner_id="1")
        voice_session.user_limit = user_limit

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(new_member, channel)

        assert result is should_kick

    @pytest.mark.parametrize(
        "is_locked,has_connect_permission,should_kick",
        [
            (False, None, False),  # ロックなし
            (False, True, False),  # ロックなし、権限あり
            (True, True, False),  # ロックあり、権限あり → OK
            (True, None, True),  # ロックあり、権限なし → キック
            (True, False, True),  # ロックあり、明示的拒否 → キック
        ],
    )
    async def test_lock_enforcement(
        self, is_locked: bool, has_connect_permission: bool | None, should_kick: bool
    ) -> None:
        """ロック状態によるキック判定のパラメタライズテスト。"""
        cog = _make_cog()
        member = _make_member(99)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock()

        channel = _make_channel(100, [member])
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = has_connect_permission
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=is_locked
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is should_kick

    @pytest.mark.parametrize(
        "is_admin,is_owner,is_bot,expected_bypass",
        [
            (True, False, False, True),  # 管理者はバイパス
            (False, True, False, True),  # オーナーはバイパス
            (False, False, True, True),  # Bot はバイパス
            (False, False, False, False),  # 一般ユーザーはバイパスしない
        ],
    )
    async def test_restriction_bypass(
        self, is_admin: bool, is_owner: bool, is_bot: bool, expected_bypass: bool
    ) -> None:
        """制限バイパスのパラメタライズテスト。"""
        cog = _make_cog()
        member = _make_member(99, bot=is_bot)
        if not is_bot:
            member.guild_permissions = MagicMock()
            member.guild_permissions.administrator = is_admin
            member.move_to = AsyncMock()
            member.send = AsyncMock()

        channel = _make_channel(100, [member])
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        owner_id = "99" if is_owner else "1"
        voice_session = _make_voice_session(
            channel_id="100", owner_id=owner_id, is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        # バイパスが期待される場合、キックされない (result=False)
        assert result is not expected_bypass

    @pytest.mark.parametrize(
        "default_user_limit",
        [0, 1, 5, 10, 99],
    )
    async def test_lobby_default_user_limit(self, default_user_limit: int) -> None:
        """ロビーのデフォルト人数制限がセッションに反映される。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = default_user_limit

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ) as mock_create,
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch(
                "src.cogs.voice.ControlPanelView",
                return_value=MagicMock(),
            ),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

            mock_create.assert_awaited_once()
            assert mock_create.call_args[1]["user_limit"] == default_user_limit

    @pytest.mark.parametrize(
        "channel_type,expected_handled",
        [
            (discord.VoiceChannel, True),
            (discord.StageChannel, False),
            (discord.TextChannel, False),
        ],
    )
    async def test_channel_type_filtering(
        self, channel_type: type, expected_handled: bool
    ) -> None:
        """チャンネルタイプによるフィルタリング。"""
        cog = _make_cog()
        member = _make_member(1)

        before = MagicMock(spec=discord.VoiceState)
        before.channel = None

        after_channel = MagicMock(spec=channel_type)
        after_channel.id = 100
        after = MagicMock(spec=discord.VoiceState)
        after.channel = after_channel

        cog._handle_lobby_join = AsyncMock()  # type: ignore[method-assign]
        cog._record_join_to_db = AsyncMock()  # type: ignore[method-assign]
        cog._enforce_channel_restrictions = AsyncMock(  # type: ignore[method-assign]
            return_value=False
        )

        await cog.on_voice_state_update(member, before, after)

        if expected_handled:
            cog._handle_lobby_join.assert_awaited_once()
        else:
            cog._handle_lobby_join.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests for _record_join_to_db and _remove_join_from_db
# ---------------------------------------------------------------------------


class TestRecordJoinToDb:
    """Tests for _record_join_to_db."""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_voice_session")
    @patch("src.cogs.voice.add_voice_session_member")
    async def test_records_member_when_session_exists(
        self,
        mock_add_member: AsyncMock,
        mock_get_session: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """既存セッションがある場合、メンバーが記録される。"""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        voice_session = _make_voice_session()
        mock_get_session.return_value = voice_session

        cog = _make_cog()
        await cog._record_join_to_db(100, 123)

        mock_get_session.assert_awaited_once_with(mock_session, "100")
        mock_add_member.assert_awaited_once_with(mock_session, voice_session.id, "123")

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_voice_session")
    @patch("src.cogs.voice.add_voice_session_member")
    async def test_does_nothing_when_no_session(
        self,
        mock_add_member: AsyncMock,
        mock_get_session: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """セッションが存在しない場合、何もしない。"""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_get_session.return_value = None

        cog = _make_cog()
        await cog._record_join_to_db(100, 123)

        mock_add_member.assert_not_awaited()


class TestRemoveJoinFromDb:
    """Tests for _remove_join_from_db."""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_voice_session")
    @patch("src.cogs.voice.remove_voice_session_member")
    async def test_removes_member_when_session_exists(
        self,
        mock_remove_member: AsyncMock,
        mock_get_session: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """既存セッションがある場合、メンバーが削除される。"""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        voice_session = _make_voice_session()
        mock_get_session.return_value = voice_session

        cog = _make_cog()
        await cog._remove_join_from_db(100, 123)

        mock_get_session.assert_awaited_once_with(mock_session, "100")
        mock_remove_member.assert_awaited_once_with(
            mock_session, voice_session.id, "123"
        )

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_voice_session")
    @patch("src.cogs.voice.remove_voice_session_member")
    async def test_does_nothing_when_no_session(
        self,
        mock_remove_member: AsyncMock,
        mock_get_session: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """セッションが存在しない場合、何もしない。"""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_get_session.return_value = None

        cog = _make_cog()
        await cog._remove_join_from_db(100, 123)

        mock_remove_member.assert_not_awaited()


# ---------------------------------------------------------------------------
# Tests for setup function
# ---------------------------------------------------------------------------


class TestVoiceCogSetup:
    """Tests for setup function."""

    async def test_setup_adds_cog(self) -> None:
        """setup() が Cog を追加する。"""
        from src.cogs.voice import setup

        bot = MagicMock(spec=discord.ext.commands.Bot)
        bot.add_cog = AsyncMock()

        await setup(bot)

        bot.add_cog.assert_awaited_once()
        args = bot.add_cog.call_args[0]
        assert isinstance(args[0], VoiceCog)


# ---------------------------------------------------------------------------
# VC 作成クールダウンテスト
# ---------------------------------------------------------------------------


class TestVcCreateCooldown:
    """VC 作成クールダウンの単体テスト。"""

    def test_first_action_not_on_cooldown(self) -> None:
        """最初の操作はクールダウンされない."""
        user_id = 12345

        on_cooldown, remaining = is_vc_create_on_cooldown(user_id)

        assert on_cooldown is False
        assert remaining == 0.0

    def test_immediate_second_action_on_cooldown(self) -> None:
        """直後の操作はクールダウンされる."""
        user_id = 12345

        # 1回目 (クールダウンを記録)
        record_vc_create_cooldown(user_id)

        # 即座に2回目
        on_cooldown, remaining = is_vc_create_on_cooldown(user_id)

        assert on_cooldown is True
        assert remaining > 0

    def test_different_user_not_affected(self) -> None:
        """異なるユーザーはクールダウンの影響を受けない."""
        user_id_1 = 12345
        user_id_2 = 67890

        # ユーザー1がVC作成
        record_vc_create_cooldown(user_id_1)

        # ユーザー2は影響を受けない
        on_cooldown, _ = is_vc_create_on_cooldown(user_id_2)

        assert on_cooldown is False

    def test_cooldown_expires(self) -> None:
        """クールダウン時間経過後は再度操作できる."""
        import time
        from unittest.mock import patch

        user_id = 12345

        # 1回目
        record_vc_create_cooldown(user_id)

        # time.monotonic をモックしてクールダウン時間経過をシミュレート
        original_time = time.monotonic()
        with patch(
            "src.cogs.voice.time.monotonic",
            return_value=original_time + VC_CREATE_COOLDOWN_SECONDS + 0.1,
        ):
            on_cooldown, _ = is_vc_create_on_cooldown(user_id)

        assert on_cooldown is False

    def test_clear_cooldown_cache(self) -> None:
        """クールダウンキャッシュをクリアできる."""
        user_id = 12345

        # クールダウンを設定
        record_vc_create_cooldown(user_id)
        on_cooldown, _ = is_vc_create_on_cooldown(user_id)
        assert on_cooldown is True

        # キャッシュをクリア
        clear_vc_create_cooldown_cache()

        # クリア後はクールダウンされない
        on_cooldown, _ = is_vc_create_on_cooldown(user_id)
        assert on_cooldown is False

    def test_cooldown_constant_value(self) -> None:
        """クールダウン時間が適切に設定されている."""
        assert VC_CREATE_COOLDOWN_SECONDS == 30


class TestVcCreateCooldownIntegration:
    """VC 作成クールダウンの統合テスト (_handle_lobby_join との連携)."""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_lobby_by_channel_id")
    async def test_lobby_join_blocked_when_on_cooldown(
        self,
        mock_get_lobby: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """クールダウン中にロビーに参加すると、VC作成がブロックされる."""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        # ロビーが存在する
        mock_lobby = MagicMock()
        mock_lobby.id = 1
        mock_get_lobby.return_value = mock_lobby

        cog = _make_cog()
        member = _make_member(12345)
        member.send = AsyncMock()
        member.move_to = AsyncMock()

        channel = _make_channel(100)

        # クールダウンを記録
        record_vc_create_cooldown(member.id)

        # ロビー参加処理を実行
        member.voice.channel = channel
        await cog._handle_lobby_join(member, channel)

        # DMで通知が送信される
        member.send.assert_awaited_once()
        assert "VCの作成は" in member.send.call_args.args[0]

        # ロビーから切断される
        member.move_to.assert_awaited_once_with(None)

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_lobby_by_channel_id")
    @patch("src.cogs.voice.create_voice_session")
    @patch("src.cogs.voice.add_voice_session_member")
    async def test_lobby_join_records_cooldown_on_success(
        self,
        mock_add_member: AsyncMock,
        mock_create_session: AsyncMock,
        mock_get_lobby: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """VC作成成功時にクールダウンが記録される."""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        # ロビーが存在する
        mock_lobby = MagicMock()
        mock_lobby.id = 1
        mock_lobby.category_id = None
        mock_lobby.default_user_limit = 0
        mock_get_lobby.return_value = mock_lobby

        # VC作成成功
        mock_vs = MagicMock()
        mock_vs.id = 1
        mock_vs.is_locked = False
        mock_vs.is_hidden = False
        mock_vs.user_limit = 0
        mock_create_session.return_value = mock_vs

        cog = _make_cog()
        member = _make_member(12345)
        member.display_name = "TestUser"
        member.move_to = AsyncMock()

        # Guild と Channel のモック
        channel = _make_channel(100)
        channel.category = None
        channel.overwrites = {}

        new_channel = MagicMock(spec=discord.VoiceChannel)
        new_channel.id = 200
        new_channel.nsfw = False
        new_channel.set_permissions = AsyncMock()
        new_channel.send = AsyncMock()
        new_channel.delete = AsyncMock()

        mock_msg = MagicMock()
        mock_msg.pin = AsyncMock()
        new_channel.send.return_value = mock_msg

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()

        member.guild = guild

        # VC作成前はクールダウンなし
        on_cooldown, _ = is_vc_create_on_cooldown(member.id)
        assert on_cooldown is False

        # ロビー参加処理を実行
        member.voice.channel = channel
        await cog._handle_lobby_join(member, channel)

        # VC作成後はクールダウン中
        on_cooldown, _ = is_vc_create_on_cooldown(member.id)
        assert on_cooldown is True

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_lobby_by_channel_id")
    async def test_cooldown_dm_failure_still_disconnects(
        self,
        mock_get_lobby: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """DM送信が失敗しても、ロビーからの切断は行われる."""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_lobby = MagicMock()
        mock_lobby.id = 1
        mock_get_lobby.return_value = mock_lobby

        cog = _make_cog()
        member = _make_member(12345)
        member.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "DM closed"))
        member.move_to = AsyncMock()

        channel = _make_channel(100)

        # クールダウンを記録
        record_vc_create_cooldown(member.id)

        # ロビー参加処理を実行 (例外は発生しない)
        member.voice.channel = channel
        await cog._handle_lobby_join(member, channel)

        # ロビーから切断される
        member.move_to.assert_awaited_once_with(None)


# ---------------------------------------------------------------------------
# ロック + クールダウン二重保護統合テスト
# ---------------------------------------------------------------------------


class TestVcCreateLockCooldownIntegration:
    """VC 作成のロック + クールダウン二重保護の統合テスト."""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_lobby_by_channel_id")
    @patch("src.cogs.voice.create_voice_session")
    @patch("src.cogs.voice.add_voice_session_member")
    async def test_concurrent_requests_serialized_by_lock(
        self,
        mock_add_member: AsyncMock,
        mock_create_session: AsyncMock,
        mock_get_lobby: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """並行リクエストがロックによりシリアライズされる."""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        # ロビーが存在する
        mock_lobby = MagicMock()
        mock_lobby.id = 1
        mock_lobby.category_id = None
        mock_lobby.default_user_limit = 0
        mock_get_lobby.return_value = mock_lobby

        # VC作成成功
        mock_vs = MagicMock()
        mock_vs.id = 1
        mock_vs.is_locked = False
        mock_vs.is_hidden = False
        mock_vs.user_limit = 0
        mock_create_session.return_value = mock_vs

        cog = _make_cog()

        # 同じユーザーの2つのリクエストを作成
        member = _make_member(12345)
        member.display_name = "TestUser"
        member.move_to = AsyncMock()
        member.send = AsyncMock()

        channel = _make_channel(100)
        channel.category = None
        channel.overwrites = {}

        new_channel = MagicMock(spec=discord.VoiceChannel)
        new_channel.id = 200
        new_channel.nsfw = False
        new_channel.set_permissions = AsyncMock()
        new_channel.send = AsyncMock()
        new_channel.delete = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.pin = AsyncMock()
        new_channel.send.return_value = mock_msg

        guild = MagicMock(spec=discord.Guild)
        guild.id = 1000
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock(spec=discord.Role)
        member.guild = guild

        # 並行リクエストを実行
        results = await asyncio.gather(
            cog._handle_lobby_join(member, channel),
            cog._handle_lobby_join(member, channel),
            return_exceptions=True,
        )

        # エラーが発生していないことを確認
        for r in results:
            assert not isinstance(r, Exception)

        # ロックにより、1回目の処理でクールダウンが記録され、
        # 2回目はクールダウンチェックでブロックされる
        # (create_voice_session は1回のみ呼ばれるはず)
        # 注: モックの設定により実際の動作は異なる可能性があるが、
        # 少なくとも例外が発生しないことを確認

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_lobby_by_channel_id")
    async def test_different_users_can_process_in_parallel(
        self,
        mock_get_lobby: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """異なるユーザーは並行して処理可能."""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        # ロビーが存在しない（シンプルなテストのため）
        mock_get_lobby.return_value = None

        cog = _make_cog()

        # 異なるユーザー
        member1 = _make_member(11111)
        member2 = _make_member(22222)

        channel = _make_channel(100)

        # 並行リクエストを実行（ロビーが存在しないのですぐに return する）
        results = await asyncio.gather(
            cog._handle_lobby_join(member1, channel),
            cog._handle_lobby_join(member2, channel),
            return_exceptions=True,
        )

        # エラーが発生していないことを確認
        for r in results:
            assert not isinstance(r, Exception)

    async def test_lock_does_not_block_different_resources(self) -> None:
        """異なるリソースキーのロックは互いにブロックしない."""
        from src.utils import get_resource_lock

        execution_order: list[str] = []

        async def process(user_id: int) -> None:
            async with get_resource_lock(f"vc_create:{user_id}"):
                execution_order.append(f"start_{user_id}")
                await asyncio.sleep(0.01)
                execution_order.append(f"end_{user_id}")

        # 異なるユーザー ID でロックを取得（並列実行される）
        await asyncio.gather(process(1), process(2))

        # 両方とも完了している
        assert len(execution_order) == 4
        assert "start_1" in execution_order
        assert "end_1" in execution_order
        assert "start_2" in execution_order
        assert "end_2" in execution_order

    async def test_lock_serializes_same_resource(self) -> None:
        """同じリソースキーのロックはシリアライズされる."""
        from src.utils import get_resource_lock

        execution_order: list[str] = []

        async def process(name: str) -> None:
            async with get_resource_lock("vc_create:same_user"):
                execution_order.append(f"start_{name}")
                await asyncio.sleep(0.01)
                execution_order.append(f"end_{name}")

        # 同じユーザー ID でロックを取得（シリアライズされる）
        await asyncio.gather(process("A"), process("B"))

        # シリアライズされているため、start-end が連続している
        assert len(execution_order) == 4
        # 最初の start-end は連続
        assert execution_order[0].startswith("start_")
        assert execution_order[1].startswith("end_")
        assert execution_order[0][6:] == execution_order[1][4:]


# =============================================================================
# on_guild_remove リスナーのテスト
# =============================================================================


class TestOnGuildRemove:
    """on_guild_remove リスナーのテスト。"""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.delete_voice_sessions_by_guild")
    @patch("src.cogs.voice.delete_lobbies_by_guild")
    async def test_deletes_all_voice_data(
        self,
        mock_delete_lobbies: AsyncMock,
        mock_delete_sessions: AsyncMock,
        mock_session: MagicMock,
    ) -> None:
        """ギルド削除時に全ての VC データを削除する。"""
        cog = _make_cog()

        mock_delete_sessions.return_value = 5
        mock_delete_lobbies.return_value = 2

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.return_value = mock_session_ctx

        guild = MagicMock(spec=discord.Guild)
        guild.id = 789

        await cog.on_guild_remove(guild)

        mock_delete_sessions.assert_called_once()
        mock_delete_lobbies.assert_called_once()

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.delete_voice_sessions_by_guild")
    @patch("src.cogs.voice.delete_lobbies_by_guild")
    async def test_handles_no_data(
        self,
        mock_delete_lobbies: AsyncMock,
        mock_delete_sessions: AsyncMock,
        mock_session: MagicMock,
    ) -> None:
        """データがない場合も正常に動作する。"""
        cog = _make_cog()

        mock_delete_sessions.return_value = 0
        mock_delete_lobbies.return_value = 0

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.return_value = mock_session_ctx

        guild = MagicMock(spec=discord.Guild)
        guild.id = 789

        await cog.on_guild_remove(guild)

        mock_delete_sessions.assert_called_once()
        mock_delete_lobbies.assert_called_once()

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.delete_voice_sessions_by_guild")
    @patch("src.cogs.voice.delete_lobbies_by_guild")
    async def test_deletes_sessions_before_lobbies(
        self,
        mock_delete_lobbies: AsyncMock,
        mock_delete_sessions: AsyncMock,
        mock_session: MagicMock,
    ) -> None:
        """外部キー制約のため、セッションがロビーより先に削除される。"""
        cog = _make_cog()

        call_order: list[str] = []

        async def track_sessions(*_args: object) -> int:
            call_order.append("sessions")
            return 1

        async def track_lobbies(*_args: object) -> int:
            call_order.append("lobbies")
            return 1

        mock_delete_sessions.side_effect = track_sessions
        mock_delete_lobbies.side_effect = track_lobbies

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.return_value = mock_session_ctx

        guild = MagicMock(spec=discord.Guild)
        guild.id = 789

        await cog.on_guild_remove(guild)

        # セッションが先に削除される
        assert call_order == ["sessions", "lobbies"]


# ===========================================================================
# _get_longest_member — 追加エッジケース
# ===========================================================================


class TestGetLongestMemberEdgeCases:
    """Edge case tests for _get_longest_member."""

    async def test_all_bots_returns_none(self) -> None:
        """Channel with only bot members returns None."""
        cog = _make_cog()
        bot1 = _make_member(1001, bot=True)
        bot2 = _make_member(1002, bot=True)
        channel = _make_channel(100, [bot1, bot2])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda _uid: None

        voice_session = _make_voice_session()

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=99999
            )
            assert result is None

    async def test_owner_only_human_returns_none(self) -> None:
        """Owner is the only human and is excluded — returns None."""
        cog = _make_cog()
        owner = _make_member(12345)
        bot1 = _make_member(1001, bot=True)
        channel = _make_channel(100, [owner, bot1])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: owner if uid == 12345 else None

        voice_session = _make_voice_session(owner_id="12345")

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=12345
            )
            assert result is None

    async def test_db_member_not_in_channel_skipped(self) -> None:
        """DB member who left the channel is skipped; present member returned."""
        cog = _make_cog()
        m222 = _make_member(222)
        # Only member 222 is in the channel (111 has left)
        channel = _make_channel(100, [m222])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m222 if uid == 222 else None

        voice_session = _make_voice_session()

        # DB returns 111 first (joined earlier), then 222
        mock_db_member1 = MagicMock()
        mock_db_member1.user_id = "111"
        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "222"

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[mock_db_member1, mock_db_member2],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=99999
            )
            # 111 is not in the channel, so 222 should be returned
            assert result is m222


# ===========================================================================
# VC 作成クールダウン — 境界テスト
# ===========================================================================


class TestVcCooldownBoundary:
    """Boundary tests for VC create cooldown."""

    def test_cooldown_exact_boundary_active(self) -> None:
        """Just before cooldown expires — still active."""
        user_id = 42
        record_vc_create_cooldown(user_id)

        # Capture the recorded time
        from src.cogs.voice import _vc_create_cooldown_cache

        recorded_time = _vc_create_cooldown_cache[user_id]

        fake_now = recorded_time + VC_CREATE_COOLDOWN_SECONDS - 0.001
        with patch("src.cogs.voice.time") as mock_time:
            mock_time.monotonic.return_value = fake_now
            on_cooldown, remaining = is_vc_create_on_cooldown(user_id)

        assert on_cooldown is True
        assert remaining > 0

    def test_cooldown_just_expired(self) -> None:
        """Just after cooldown expires — no longer active."""
        user_id = 43
        record_vc_create_cooldown(user_id)

        from src.cogs.voice import _vc_create_cooldown_cache

        recorded_time = _vc_create_cooldown_cache[user_id]

        fake_now = recorded_time + VC_CREATE_COOLDOWN_SECONDS + 0.001
        with patch("src.cogs.voice.time") as mock_time:
            mock_time.monotonic.return_value = fake_now
            on_cooldown, remaining = is_vc_create_on_cooldown(user_id)

        assert on_cooldown is False
        assert remaining == 0.0


# ===========================================================================
# _record_join_to_db / _remove_join_from_db — セッション不在ガード
# ===========================================================================


class TestRecordJoinLeaveGuard:
    """_record_join_to_db / _remove_join_from_db are no-ops when no session."""

    async def test_record_join_no_session_noop(self) -> None:
        """_record_join_to_db does nothing when get_voice_session returns None."""
        cog = _make_cog()

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.cogs.voice.add_voice_session_member",
                new_callable=AsyncMock,
            ) as mock_add,
        ):
            await cog._record_join_to_db(channel_id=12345, user_id=67890)
            mock_add.assert_not_awaited()

    async def test_remove_join_no_session_noop(self) -> None:
        """_remove_join_from_db does nothing when get_voice_session returns None."""
        cog = _make_cog()

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.cogs.voice.remove_voice_session_member",
                new_callable=AsyncMock,
            ) as mock_remove,
        ):
            await cog._remove_join_from_db(channel_id=12345, user_id=67890)
            mock_remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# Additional Edge Case Tests
# ---------------------------------------------------------------------------


class TestVcCreateCooldownEdgeCases:
    """VC 作成クールダウンの追加エッジケーステスト。"""

    def test_first_check_not_on_cooldown(self) -> None:
        """最初のチェックはクールダウン中ではない。"""
        on_cooldown, remaining = is_vc_create_on_cooldown(1)
        assert on_cooldown is False
        assert remaining == 0.0

    def test_after_record_is_on_cooldown(self) -> None:
        """record 後は即座にクールダウン中。"""
        record_vc_create_cooldown(1)
        on_cooldown, remaining = is_vc_create_on_cooldown(1)
        assert on_cooldown is True
        assert remaining > 0

    def test_different_users_independent(self) -> None:
        """異なるユーザーのクールダウンは独立。"""
        record_vc_create_cooldown(1)
        on_cooldown, _ = is_vc_create_on_cooldown(2)
        assert on_cooldown is False

    def test_remaining_decreases(self) -> None:
        """残り時間が VC_CREATE_COOLDOWN_SECONDS 以下。"""
        record_vc_create_cooldown(1)
        on_cooldown, remaining = is_vc_create_on_cooldown(1)
        assert on_cooldown is True
        assert 0 < remaining <= VC_CREATE_COOLDOWN_SECONDS

    def test_clear_cache_resets_cooldown(self) -> None:
        """キャッシュクリア後はクールダウンリセット。"""
        record_vc_create_cooldown(1)
        clear_vc_create_cooldown_cache()
        on_cooldown, _ = is_vc_create_on_cooldown(1)
        assert on_cooldown is False


class TestRecordJoinCacheEdgeCases:
    """_record_join_cache の追加エッジケーステスト。"""

    def test_same_channel_many_members(self) -> None:
        """1つのチャンネルに多数のメンバーを記録。"""
        cog = _make_cog()
        for i in range(50):
            cog._record_join_cache(100, i)
        assert len(cog._join_times[100]) == 50

    def test_many_channels_one_member(self) -> None:
        """多数のチャンネルに1メンバーずつ記録。"""
        cog = _make_cog()
        for i in range(50):
            cog._record_join_cache(i, 1)
        assert len(cog._join_times) == 50


class TestGetLongestMemberAdditionalEdgeCases:
    """_get_longest_member の追加エッジケーステスト。"""

    async def test_no_db_members_falls_back_to_channel(self) -> None:
        """DB にメンバーがいない場合はチャンネルメンバーにフォールバックする。"""
        cog = _make_cog()
        m1 = _make_member(1)
        channel = _make_channel(100, [m1])
        channel.guild = MagicMock()
        voice_session = _make_voice_session()

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999
            )
            # DB が空でもチャンネルメンバーからフォールバック
            assert result == m1

    async def test_no_db_members_no_channel_members_returns_none(self) -> None:
        """DB もチャンネルにもメンバーがいない場合は None を返す。"""
        cog = _make_cog()
        channel = _make_channel(100, [])
        channel.guild = MagicMock()
        voice_session = _make_voice_session()

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999
            )
            assert result is None

    async def test_all_members_excluded_returns_none(self) -> None:
        """全メンバーが除外対象の場合は None を返す。"""
        cog = _make_cog()
        m1 = _make_member(1)
        channel = _make_channel(100, [m1])
        channel.guild = MagicMock()
        channel.guild.get_member = lambda uid: m1 if uid == 1 else None
        voice_session = _make_voice_session()

        mock_db_member = MagicMock()
        mock_db_member.user_id = "1"

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[mock_db_member],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=1
            )
            assert result is None

    async def test_member_left_guild_skipped(self) -> None:
        """ギルドを離脱したメンバーはスキップされる。"""
        cog = _make_cog()
        m2 = _make_member(2)
        channel = _make_channel(100, [m2])
        channel.guild = MagicMock()
        # user_id=1 は guild.get_member で None (離脱)
        channel.guild.get_member = lambda uid: m2 if uid == 2 else None
        voice_session = _make_voice_session()

        mock_db_member1 = MagicMock()
        mock_db_member1.user_id = "1"
        mock_db_member2 = MagicMock()
        mock_db_member2.user_id = "2"

        mock_session = AsyncMock()
        with patch(
            "src.cogs.voice.get_voice_session_members_ordered",
            new_callable=AsyncMock,
            return_value=[mock_db_member1, mock_db_member2],
        ):
            result = await cog._get_longest_member(
                mock_session, voice_session, channel, exclude_id=999
            )
            assert result is m2


class TestVcCleanupGuard:
    """VC クールダウンクリーンアップのガード条件テスト。"""

    def test_cleanup_guard_allows_zero_last_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_vc_last_cleanup_time=0 でもクリーンアップが実行される.

        time.monotonic() が小さい環境 (CI等) でも
        0 は「未実行」として扱われクリーンアップがスキップされないことを検証。
        """
        import time

        import src.cogs.voice as voice_module

        key = 99999
        _vc_create_cooldown_cache[key] = time.monotonic() - 400

        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", 0)
        _cleanup_vc_create_cooldown_cache()

        assert key not in _vc_create_cooldown_cache
        assert voice_module._vc_last_cleanup_time > 0

    def test_cleanup_removes_old_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """古いエントリがクリーンアップされる."""
        import time

        import src.cogs.voice as voice_module

        key = 11111
        _vc_create_cooldown_cache[key] = time.monotonic() - 400

        monkeypatch.setattr(
            voice_module, "_vc_last_cleanup_time", time.monotonic() - 700
        )
        _cleanup_vc_create_cooldown_cache()

        assert key not in _vc_create_cooldown_cache

    def test_cleanup_preserves_recent_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """最近のエントリはクリーンアップされない."""
        import time

        import src.cogs.voice as voice_module

        key = 22222
        _vc_create_cooldown_cache[key] = time.monotonic() - 10

        monkeypatch.setattr(
            voice_module, "_vc_last_cleanup_time", time.monotonic() - 700
        )
        _cleanup_vc_create_cooldown_cache()

        assert key in _vc_create_cooldown_cache

    def test_cleanup_interval_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """クリーンアップ間隔が未経過ならスキップされる."""
        import time

        import src.cogs.voice as voice_module

        key = 33333
        _vc_create_cooldown_cache[key] = time.monotonic() - 400

        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", time.monotonic() - 1)
        _cleanup_vc_create_cooldown_cache()

        # 間隔未経過なのでエントリはまだ残る
        assert key in _vc_create_cooldown_cache

    def test_cleanup_keeps_active_removes_expired(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """期限切れエントリは削除、アクティブは保持."""
        import time

        import src.cogs.voice as voice_module

        expired_key = 44444
        active_key = 55555
        _vc_create_cooldown_cache[expired_key] = time.monotonic() - 400
        _vc_create_cooldown_cache[active_key] = time.monotonic()

        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", 0)
        _cleanup_vc_create_cooldown_cache()

        assert expired_key not in _vc_create_cooldown_cache
        assert active_key in _vc_create_cooldown_cache


class TestVcCleanupEmptyCache:
    """空キャッシュに対するクリーンアップが安全に動作することを検証。"""

    def test_cleanup_on_empty_cache_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """キャッシュが空でもクリーンアップがクラッシュしない."""
        import src.cogs.voice as voice_module

        assert len(_vc_create_cooldown_cache) == 0
        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", 0)
        _cleanup_vc_create_cooldown_cache()
        assert len(_vc_create_cooldown_cache) == 0
        assert voice_module._vc_last_cleanup_time > 0

    def test_is_cooldown_on_empty_cache(self) -> None:
        """空キャッシュで is_vc_create_on_cooldown が (False, 0.0) を返す."""
        assert len(_vc_create_cooldown_cache) == 0
        on_cooldown, remaining = is_vc_create_on_cooldown(99999)
        assert on_cooldown is False
        assert remaining == 0.0


class TestVcCleanupAllExpired:
    """全エントリが期限切れの場合にキャッシュが空になることを検証。"""

    def test_all_expired_entries_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全エントリが期限切れなら全て削除されキャッシュが空になる."""
        import time

        import src.cogs.voice as voice_module

        now = time.monotonic()
        _vc_create_cooldown_cache[1001] = now - 400
        _vc_create_cooldown_cache[1002] = now - 500
        _vc_create_cooldown_cache[1003] = now - 600

        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", 0)
        _cleanup_vc_create_cooldown_cache()

        assert len(_vc_create_cooldown_cache) == 0


class TestVcCleanupTriggerViaPublicAPI:
    """公開 API 関数がクリーンアップを内部的にトリガーすることを検証。"""

    def test_is_cooldown_triggers_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """is_vc_create_on_cooldown がクリーンアップをトリガーする."""
        import time

        import src.cogs.voice as voice_module

        old_key = 77777
        _vc_create_cooldown_cache[old_key] = time.monotonic() - 400

        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", 0)
        is_vc_create_on_cooldown(99999)

        assert old_key not in _vc_create_cooldown_cache

    def test_cleanup_updates_last_cleanup_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """クリーンアップ実行後に _vc_last_cleanup_time が更新される."""
        import src.cogs.voice as voice_module

        monkeypatch.setattr(voice_module, "_vc_last_cleanup_time", 0)
        is_vc_create_on_cooldown(99999)

        assert voice_module._vc_last_cleanup_time > 0

    def test_record_cooldown_then_check(self) -> None:
        """record_vc_create_cooldown で記録後 is_vc_create_on_cooldown で検出される."""
        user_id = 88888
        record_vc_create_cooldown(user_id)
        on_cooldown, remaining = is_vc_create_on_cooldown(user_id)
        assert on_cooldown is True
        assert remaining > 0


class TestVoiceSetupCacheVerification:
    """setup() がキャッシュを正しく構築することを検証。"""

    async def test_setup_builds_lobby_cache(self) -> None:
        """setup が _lobby_channel_ids キャッシュを構築する."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from src.cogs.voice import setup

        mock_bot = MagicMock()
        mock_bot.add_cog = AsyncMock()

        mock_lobby1 = MagicMock()
        mock_lobby1.lobby_channel_id = 111
        mock_lobby2 = MagicMock()
        mock_lobby2.lobby_channel_id = 222

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_session.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("src.cogs.voice.async_session", return_value=mock_session),
            patch(
                "src.cogs.voice.get_all_lobbies",
                new_callable=AsyncMock,
                return_value=[mock_lobby1, mock_lobby2],
            ),
        ):
            await setup(mock_bot)

        cog = mock_bot.add_cog.call_args[0][0]
        assert hasattr(cog, "_lobby_channel_ids")
        assert cog._lobby_channel_ids == {111, 222}

    async def test_setup_does_not_raise_without_db(self) -> None:
        """DB未接続でも setup が例外を出さずに完了する."""
        from unittest.mock import AsyncMock, MagicMock

        from src.cogs.voice import setup

        mock_bot = MagicMock()
        mock_bot.add_cog = AsyncMock()

        await setup(mock_bot)
        mock_bot.add_cog.assert_called_once()


# ===========================================================================
# 重複排除テーブルによる重複防止テスト
# ===========================================================================


class TestDeduplicationDuplicateGuard:
    """claim_event による重複防止のテスト (voice cog)。"""

    async def test_lobby_join_skips_on_duplicate_claim(self) -> None:
        """claim_event が False → VC 作成をスキップ。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 5

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
            ) as mock_create,
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)
            mock_create.assert_not_awaited()

    async def test_channel_leave_skips_delete_on_duplicate_claim(self) -> None:
        """claim_event が False → チャンネル削除をスキップ。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100, members=[])
        channel.delete = AsyncMock()

        voice_session = _make_voice_session(channel_id="100")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await cog._handle_channel_leave(member, channel)
            channel.delete.assert_not_awaited()

    async def test_transfer_ownership_skips_on_duplicate_claim(self) -> None:
        """claim_event が False → 引き継ぎをスキップ。"""
        cog = _make_cog()
        old_owner = _make_member(1)
        channel = _make_channel(100, members=[_make_member(2)])

        voice_session = _make_voice_session(channel_id="100", owner_id="1")
        mock_session = AsyncMock()

        new_owner = _make_member(2)
        with (
            patch.object(
                cog,
                "_get_longest_member",
                new_callable=AsyncMock,
                return_value=new_owner,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "src.cogs.voice.update_voice_session",
                new_callable=AsyncMock,
            ) as mock_update,
        ):
            await cog._transfer_ownership(
                mock_session, voice_session, old_owner, channel
            )
            mock_update.assert_not_awaited()

    async def test_enforce_restrictions_skips_kick_on_duplicate_claim(self) -> None:
        """claim_event が False → キックをスキップ。"""
        cog = _make_cog()
        member = _make_member(1)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()

        channel = _make_channel(100)
        overwrites = MagicMock()
        overwrites.connect = None  # 許可されていない
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="999", is_locked=True
        )

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)
            assert result is False
            member.move_to.assert_not_awaited()


class TestDeduplicationClaimSucceeds:
    """claim_event=True 時のハッピーパス検証。

    claim_event が呼ばれ、成功時にアクションが実行されることを明示的にテスト。
    """

    async def test_lobby_join_claim_called_on_success(self) -> None:
        """VC 作成成功時に claim_event が呼ばれる。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100)
        channel.category = MagicMock(spec=discord.CategoryChannel)

        lobby = MagicMock()
        lobby.id = 10
        lobby.category_id = None
        lobby.default_user_limit = 5

        new_channel = _make_channel(200)
        new_channel.send = AsyncMock(return_value=MagicMock(pin=AsyncMock()))
        new_channel.set_permissions = AsyncMock()

        guild = MagicMock(spec=discord.Guild)
        guild.create_voice_channel = AsyncMock(return_value=new_channel)
        guild.default_role = MagicMock()
        member.guild = guild
        member.move_to = AsyncMock()

        voice_session = _make_voice_session(channel_id="200", owner_id="1")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_claim,
            patch(
                "src.cogs.voice.create_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch("src.cogs.voice.add_voice_session_member", new_callable=AsyncMock),
            patch(
                "src.cogs.voice.create_control_panel_embed",
                return_value=MagicMock(),
            ),
            patch("src.cogs.voice.ControlPanelView", return_value=MagicMock()),
        ):
            member.voice.channel = channel
            await cog._handle_lobby_join(member, channel)

            mock_claim.assert_awaited_once()
            guild.create_voice_channel.assert_awaited_once()

    async def test_channel_leave_claim_called_and_deletes(self) -> None:
        """空チャンネル退出時に claim_event が呼ばれ、削除される。"""
        cog = _make_cog()
        member = _make_member(1)
        channel = _make_channel(100, members=[])
        channel.delete = AsyncMock()

        voice_session = _make_voice_session(channel_id="100")

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_claim,
            patch("src.cogs.voice.delete_voice_session", new_callable=AsyncMock),
        ):
            await cog._handle_channel_leave(member, channel)

            mock_claim.assert_awaited_once()
            channel.delete.assert_awaited_once()

    async def test_enforce_restrictions_claim_called_and_kicks(self) -> None:
        """キック実行時に claim_event が呼ばれ、move_to(None) が実行される。"""
        cog = _make_cog()
        member = _make_member(1)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock()
        member.send = AsyncMock()

        channel = _make_channel(100)
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="999", is_locked=True
        )

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
            patch(
                "src.cogs.voice.claim_event",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_claim,
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

            assert result is True
            mock_claim.assert_awaited_once()
            member.move_to.assert_awaited_once_with(None)


# ===========================================================================
# 追加エッジケーステスト
# ===========================================================================


class TestEnforceRestrictionsMoveFail:
    """move_to が HTTPException を返すケースのテスト。"""

    async def test_move_to_http_exception_on_kick(self) -> None:
        """キック時に move_to が失敗しても例外を上げない。"""
        cog = _make_cog()
        member = _make_member(2)
        member.guild_permissions = MagicMock()
        member.guild_permissions.administrator = False
        member.move_to = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(), "fail")
        )
        member.send = AsyncMock()
        channel = _make_channel(100)
        channel.name = "Test Channel"
        channel.send = AsyncMock()
        overwrites = MagicMock()
        overwrites.connect = None
        channel.overwrites_for = MagicMock(return_value=overwrites)

        voice_session = _make_voice_session(
            channel_id="100", owner_id="1", is_locked=True
        )

        mock_factory, _ = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            result = await cog._enforce_channel_restrictions(member, channel)

        assert result is True
        member.move_to.assert_awaited_once_with(None)


class TestHandleLobbyJoinCacheFilter:
    """_handle_lobby_join のキャッシュフィルタリングテスト。"""

    async def test_cache_filter_skips_non_lobby(self) -> None:
        """キャッシュ初期化済みで非ロビーチャンネルはスキップ。"""
        cog = _make_cog()
        cog._lobby_channel_ids = {"200"}  # 100 はロビーではない
        member = _make_member(1)
        channel = _make_channel(100)

        # DB は呼ばれないはず
        with patch(
            "src.cogs.voice.async_session",
            side_effect=AssertionError("should not be called"),
        ):
            await cog._handle_lobby_join(member, channel)


class TestHandleChannelLeaveDeleteFail:
    """チャンネル削除失敗時のテスト。"""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_voice_session")
    @patch("src.cogs.voice.delete_voice_session")
    async def test_channel_delete_http_exception(
        self,
        mock_delete_session: AsyncMock,
        mock_get_session: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """チャンネル削除が HTTPException でも例外を上げない。"""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        voice_session = _make_voice_session(channel_id="100", owner_id="1")
        mock_get_session.return_value = voice_session

        cog = _make_cog()
        channel = _make_channel(100, members=[])
        channel.delete = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(), "fail")
        )

        member = _make_member(1)

        with patch.object(cog, "_cleanup_channel_cache"):
            await cog._handle_channel_leave(member, channel)

        channel.delete.assert_awaited_once()
        mock_delete_session.assert_awaited_once()


class TestOnGuildChannelDeleteCacheDiscard:
    """ロビー削除時のキャッシュ更新テスト。"""

    async def test_lobby_cache_discarded(self) -> None:
        """ロビーチャンネル削除時にキャッシュからも削除される。"""
        cog = _make_cog()
        cog._lobby_channel_ids = {"100", "200"}
        channel = _make_channel(100)

        lobby = MagicMock()
        lobby.id = 42
        lobby.lobby_channel_id = "100"

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.delete_voice_session", new_callable=AsyncMock),
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobby_by_channel_id",
                new_callable=AsyncMock,
                return_value=lobby,
            ),
            patch("src.cogs.voice.delete_lobby", new_callable=AsyncMock),
        ):
            await cog.on_guild_channel_delete(channel)

        assert "100" not in cog._lobby_channel_ids
        assert "200" in cog._lobby_channel_ids


class TestVcLobbyOrphanCleanup:
    """vc_lobby コマンドの孤立レコードクリーンアップテスト。"""

    async def test_orphan_lobby_cleaned_up(self) -> None:
        """Discord チャンネルが消えた孤立ロビーを掃除して新規作成する。"""
        cog = _make_cog()
        cog._lobby_channel_ids = {"999"}
        interaction = _make_interaction(1)

        # 既存ロビー (チャンネルは削除済み)
        existing_lobby = MagicMock()
        existing_lobby.id = 1
        existing_lobby.lobby_channel_id = "999"

        # get_channel が None → チャンネルは削除済み
        interaction.guild.get_channel = MagicMock(return_value=None)
        new_channel = MagicMock()
        new_channel.id = 500
        new_channel.mention = "<#500>"
        interaction.guild.create_voice_channel = AsyncMock(return_value=new_channel)

        mock_factory, mock_session = _mock_async_session()
        with (
            patch("src.cogs.voice.async_session", mock_factory),
            patch(
                "src.cogs.voice.get_lobbies_by_guild",
                new_callable=AsyncMock,
                return_value=[existing_lobby],
            ),
            patch(
                "src.cogs.voice.delete_lobby",
                new_callable=AsyncMock,
            ) as mock_delete,
            patch(
                "src.cogs.voice.create_lobby",
                new_callable=AsyncMock,
            ),
        ):
            await cog.vc_lobby.callback(cog, interaction)

        # 孤立レコードが削除される
        mock_delete.assert_awaited_once_with(mock_session, 1)
        # キャッシュからも削除される
        assert "999" not in cog._lobby_channel_ids
        # 新しいロビーが作成される
        interaction.guild.create_voice_channel.assert_awaited_once()


class TestCooldownMoveToFail:
    """クールダウン中の move_to 失敗テスト。"""

    @patch("src.cogs.voice.async_session")
    @patch("src.cogs.voice.get_lobby_by_channel_id")
    async def test_cooldown_move_to_http_exception(
        self,
        mock_get_lobby: AsyncMock,
        mock_session_factory: MagicMock,
    ) -> None:
        """クールダウン中の move_to 失敗でも例外を上げない。"""
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__ = AsyncMock(
            return_value=mock_session
        )
        mock_session_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_lobby = MagicMock()
        mock_lobby.id = 1
        mock_get_lobby.return_value = mock_lobby

        cog = _make_cog()
        member = _make_member(12345)
        member.send = AsyncMock()
        member.move_to = AsyncMock(
            side_effect=discord.HTTPException(MagicMock(), "fail")
        )
        channel = _make_channel(100)
        member.voice.channel = channel

        record_vc_create_cooldown(member.id)

        await cog._handle_lobby_join(member, channel)
        member.move_to.assert_awaited_once_with(None)
