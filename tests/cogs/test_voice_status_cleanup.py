"""カテゴリ別の空室時ボイスチャンネルステータス除去テスト。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from src.cogs.voice import (
    VOICE_STATUS_CLEANUP_DEFAULT_DELAY_MINUTES,
    VoiceCog,
    _can_clear_voice_channel_status,
)

_SET_VOICE_CHANNEL_STATUS_PERMISSION = 1 << 48


def _make_cog() -> VoiceCog:
    bot = MagicMock(spec=discord.ext.commands.Bot)
    bot.user = MagicMock(spec=discord.User)
    bot.user.id = 9999
    bot.guilds = []
    return VoiceCog(bot)


def _make_voice_channel(
    *,
    channel_id: int = 100,
    category_id: int = 200,
    member_count: int = 0,
) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.category_id = category_id
    channel.members = [MagicMock(spec=discord.Member) for _ in range(member_count)]
    channel.edit = AsyncMock()
    channel.guild = MagicMock(spec=discord.Guild)
    channel.guild.id = 1000
    channel.guild.me = MagicMock(spec=discord.Member)
    return channel


def _make_category(
    channels: list[MagicMock] | None = None,
    *,
    category_id: int = 200,
) -> MagicMock:
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = category_id
    category.voice_channels = channels or []
    return category


def _make_voice_state(channel: object | None) -> MagicMock:
    state = MagicMock(spec=discord.VoiceState)
    state.channel = channel
    return state


def _mock_async_session() -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory, session


def _grant_voice_status_cleanup_permissions(channel: MagicMock) -> None:
    permissions = discord.Permissions.none()
    permissions.update(manage_channels=True)
    permissions.value |= _SET_VOICE_CHANNEL_STATUS_PERMISSION
    channel.permissions_for.return_value = permissions


class TestVoiceStatusCleanupPermission:
    """Discord の新しい権限ビットを含む権限判定テスト。"""

    def test_requires_manage_channels_and_voice_status_permission(self) -> None:
        channel = _make_voice_channel()
        permissions = discord.Permissions.none()
        permissions.update(manage_channels=True)
        channel.permissions_for.return_value = permissions

        assert _can_clear_voice_channel_status(channel, bot_user_id=9999) is False

        permissions.value |= _SET_VOICE_CHANNEL_STATUS_PERMISSION
        assert _can_clear_voice_channel_status(channel, bot_user_id=9999) is True

    def test_administrator_bypasses_individual_permissions(self) -> None:
        channel = _make_voice_channel()
        channel.permissions_for.return_value = discord.Permissions(administrator=True)

        assert _can_clear_voice_channel_status(channel, bot_user_id=9999) is True


class TestVoiceStatusCleanupStateUpdate:
    """VC人数の境界値と再入室キャンセルを検証する。"""

    @pytest.mark.parametrize("member_count", [0, 1, 2, 3, 5])
    async def test_schedules_only_when_channel_reaches_zero_members(
        self,
        member_count: int,
    ) -> None:
        cog = _make_cog()
        channel = _make_voice_channel(member_count=member_count)
        before = _make_voice_state(channel)
        after = _make_voice_state(None)
        config = MagicMock(delay_seconds=300)
        factory, session = _mock_async_session()
        cog._start_voice_status_cleanup = MagicMock()  # type: ignore[method-assign]

        with (
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.get_voice_status_cleanup_config",
                new_callable=AsyncMock,
                return_value=config,
            ) as get_config,
        ):
            await cog._handle_voice_status_cleanup_state_update(before, after)

        if member_count == 0:
            get_config.assert_awaited_once_with(session, "1000", "200")
            cog._start_voice_status_cleanup.assert_called_once_with(
                channel,
                200,
                300,
                replace=False,
            )
        else:
            get_config.assert_not_awaited()
            cog._start_voice_status_cleanup.assert_not_called()

    async def test_join_cancels_pending_cleanup_without_db_lookup(self) -> None:
        cog = _make_cog()
        channel = _make_voice_channel(member_count=1)
        before = _make_voice_state(None)
        after = _make_voice_state(channel)
        cog._cancel_voice_status_cleanup = MagicMock()  # type: ignore[method-assign]

        with patch(
            "src.cogs.voice.async_session",
            side_effect=AssertionError("DB lookup is unnecessary on join"),
        ):
            await cog._handle_voice_status_cleanup_state_update(before, after)

        cog._cancel_voice_status_cleanup.assert_called_once_with(channel.id)

    async def test_listener_passes_before_and_after_to_cleanup_handler(self) -> None:
        """イベント入力を入れ替えずステータス監視へ渡す。"""
        cog = _make_cog()
        member = MagicMock(spec=discord.Member)
        member.id = 1
        before = _make_voice_state(MagicMock(spec=discord.StageChannel))
        after = _make_voice_state(MagicMock(spec=discord.StageChannel))
        cog._handle_voice_status_cleanup_state_update = AsyncMock()  # type: ignore[method-assign]
        cog._handle_voice_notify_state_update = AsyncMock()  # type: ignore[method-assign]

        await cog.on_voice_state_update(member, before, after)

        cog._handle_voice_status_cleanup_state_update.assert_awaited_once_with(
            before,
            after,
        )


class TestVoiceStatusCleanupExecution:
    """待機後の再確認と Discord API 呼び出しを検証する。"""

    async def test_clears_status_after_delay_when_channel_is_still_empty(
        self,
    ) -> None:
        cog = _make_cog()
        channel = _make_voice_channel()
        _grant_voice_status_cleanup_permissions(channel)
        guild = channel.guild
        guild.get_channel.return_value = channel
        cog.bot.get_guild.return_value = guild
        config = MagicMock(delay_seconds=300)
        factory, session = _mock_async_session()

        with (
            patch("src.cogs.voice.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.get_voice_status_cleanup_config",
                new_callable=AsyncMock,
                return_value=config,
            ) as get_config,
        ):
            await cog._clear_voice_status_after_delay(1000, 100, 200, 300)

        sleep.assert_awaited_once_with(300)
        get_config.assert_awaited_once_with(session, "1000", "200")
        channel.edit.assert_awaited_once_with(
            status=None,
            reason="空室になったボイスチャンネルのステータスを自動除去",
        )

    @pytest.mark.parametrize(
        ("member_count", "category_id"),
        [(1, 200), (0, 999)],
    )
    async def test_does_not_clear_if_channel_changed_during_delay(
        self,
        member_count: int,
        category_id: int,
    ) -> None:
        cog = _make_cog()
        channel = _make_voice_channel(
            member_count=member_count,
            category_id=category_id,
        )
        channel.guild.get_channel.return_value = channel
        cog.bot.get_guild.return_value = channel.guild

        with (
            patch("src.cogs.voice.asyncio.sleep", new_callable=AsyncMock),
            patch(
                "src.cogs.voice.async_session",
                side_effect=AssertionError("configuration should not be queried"),
            ),
        ):
            await cog._clear_voice_status_after_delay(1000, 100, 200, 300)

        channel.edit.assert_not_awaited()

    async def test_does_not_clear_without_required_permissions(self) -> None:
        cog = _make_cog()
        channel = _make_voice_channel()
        channel.permissions_for.return_value = discord.Permissions(manage_channels=True)
        channel.guild.get_channel.return_value = channel
        cog.bot.get_guild.return_value = channel.guild
        config = MagicMock(delay_seconds=300)
        factory, _ = _mock_async_session()

        with (
            patch("src.cogs.voice.asyncio.sleep", new_callable=AsyncMock),
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.get_voice_status_cleanup_config",
                new_callable=AsyncMock,
                return_value=config,
            ),
        ):
            await cog._clear_voice_status_after_delay(1000, 100, 200, 300)

        channel.edit.assert_not_awaited()

    async def test_cancelled_task_does_not_clear_status(self) -> None:
        cog = _make_cog()
        channel = _make_voice_channel()
        started = asyncio.Event()
        release = asyncio.Event()

        async def wait_before_clear(
            _guild_id: int,
            _channel_id: int,
            _category_id: int,
            _delay_seconds: int,
        ) -> None:
            started.set()
            await release.wait()

        cog._clear_voice_status_after_delay = wait_before_clear  # type: ignore[method-assign]
        cog._start_voice_status_cleanup(channel, 200, 300, replace=False)
        await started.wait()

        cog._cancel_voice_status_cleanup(channel.id)
        await asyncio.sleep(0)

        assert channel.id not in cog._voice_status_cleanup_tasks


class TestVoiceStatusCleanupLifecycle:
    """再起動復元とDiscord側チャンネル削除の連携テスト。"""

    async def test_restore_schedules_empty_channels_from_persisted_config(
        self,
    ) -> None:
        cog = _make_cog()
        category = _make_category()
        guild = MagicMock(spec=discord.Guild)
        guild.id = 1000
        guild.get_channel.return_value = category
        cog.bot.guilds = [guild]
        config = MagicMock(category_id="200", delay_seconds=300)
        factory, session = _mock_async_session()
        cog._schedule_empty_voice_channels = MagicMock()  # type: ignore[method-assign]

        with (
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.list_voice_status_cleanup_configs",
                new_callable=AsyncMock,
                return_value=[config],
            ) as list_configs,
        ):
            await cog._restore_voice_status_cleanup_tasks()

        list_configs.assert_awaited_once_with(session, "1000")
        cog._schedule_empty_voice_channels.assert_called_once_with(
            category,
            300,
            replace=False,
        )

    async def test_category_delete_cancels_tasks_and_cleans_persistence(self) -> None:
        cog = _make_cog()
        category = _make_category()
        category.guild = MagicMock(spec=discord.Guild)
        category.guild.id = 1000
        factory, session = _mock_async_session()
        cog._cancel_voice_status_cleanup_for_category = MagicMock()  # type: ignore[method-assign]

        with (
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.delete_voice_notify_by_channel",
                new_callable=AsyncMock,
            ) as delete_by_channel,
        ):
            await cog.on_guild_channel_delete(category)

        delete_by_channel.assert_awaited_once_with(session, "1000", "200")
        cog._cancel_voice_status_cleanup_for_category.assert_called_once_with(200)


class TestVoiceStatusCleanupCommands:
    """カテゴリ別設定コマンドの配線テスト。"""

    @staticmethod
    def _make_interaction() -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = MagicMock(spec=discord.Guild)
        interaction.guild_id = 1000
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        return interaction

    async def test_add_uses_default_delay_and_schedules_empty_channels(self) -> None:
        cog = _make_cog()
        interaction = self._make_interaction()
        empty_channel = _make_voice_channel()
        category = _make_category([empty_channel])
        factory, session = _mock_async_session()
        cog._schedule_empty_voice_channels = MagicMock()  # type: ignore[method-assign]

        with (
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.set_voice_status_cleanup_config",
                new_callable=AsyncMock,
            ) as set_config,
        ):
            await cog.voice_status_cleanup_add.callback(cog, interaction, category)

        delay_seconds = VOICE_STATUS_CLEANUP_DEFAULT_DELAY_MINUTES * 60
        set_config.assert_awaited_once_with(
            session,
            "1000",
            "200",
            delay_seconds,
        )
        cog._schedule_empty_voice_channels.assert_called_once_with(
            category,
            delay_seconds,
            replace=True,
        )
        message = interaction.response.send_message.call_args.args[0]
        assert "0人になってから5分後" in message

    async def test_remove_deletes_config_and_cancels_category_tasks(self) -> None:
        cog = _make_cog()
        interaction = self._make_interaction()
        category = _make_category()
        factory, session = _mock_async_session()
        cog._cancel_voice_status_cleanup_for_category = MagicMock()  # type: ignore[method-assign]

        with (
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.delete_voice_status_cleanup_config",
                new_callable=AsyncMock,
                return_value=True,
            ) as delete_config,
        ):
            await cog.voice_status_cleanup_remove.callback(cog, interaction, category)

        delete_config.assert_awaited_once_with(session, "1000", "200")
        cog._cancel_voice_status_cleanup_for_category.assert_called_once_with(200)
        assert "解除しました" in interaction.response.send_message.call_args.args[0]

    async def test_status_lists_each_category_delay(self) -> None:
        cog = _make_cog()
        interaction = self._make_interaction()
        config = MagicMock(category_id="200", delay_seconds=600)
        factory, session = _mock_async_session()

        with (
            patch("src.cogs.voice.async_session", factory),
            patch(
                "src.cogs.voice.list_voice_status_cleanup_configs",
                new_callable=AsyncMock,
                return_value=[config],
            ) as list_configs,
        ):
            await cog.voice_status_cleanup_status.callback(cog, interaction)

        list_configs.assert_awaited_once_with(session, "1000")
        message = interaction.response.send_message.call_args.args[0]
        assert "<#200>" in message
        assert "10分" in message
