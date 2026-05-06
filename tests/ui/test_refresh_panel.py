"""Tests for refresh_panel_embed function."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.ui.control_panel import refresh_panel_embed


def _make_voice_session(
    *,
    session_id: int = 1,
    owner_id: str = "1",
    is_locked: bool = False,
    is_hidden: bool = False,
) -> MagicMock:
    vs = MagicMock()
    vs.id = session_id
    vs.owner_id = owner_id
    vs.is_locked = is_locked
    vs.is_hidden = is_hidden
    vs.user_limit = 0
    vs.name = "Test"
    return vs


def _mock_async_session() -> MagicMock:
    mock_session = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_factory


def _make_channel(*, owner: MagicMock | None = None) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = 100
    channel.nsfw = False
    channel.guild = MagicMock(spec=discord.Guild)
    if owner:
        channel.guild.get_member = MagicMock(return_value=owner)
    else:
        channel.guild.get_member = MagicMock(return_value=None)
    channel.guild.me = MagicMock(spec=discord.Member)
    channel.pins = AsyncMock(return_value=[])
    return channel


class TestRefreshPanelEmbed:
    """Tests for refresh_panel_embed."""

    async def test_updates_pinned_panel_message(self) -> None:
        """ピン留めされたパネルメッセージの Embed が更新される。"""
        owner = MagicMock(spec=discord.Member)
        owner.mention = "<@1>"
        channel = _make_channel(owner=owner)
        voice_session = _make_voice_session()

        # パネルメッセージのモック
        panel_msg = MagicMock()
        panel_msg.author = channel.guild.me
        panel_embed = MagicMock()
        panel_embed.title = "ボイスチャンネル設定"
        panel_msg.embeds = [panel_embed]
        panel_msg.edit = AsyncMock()
        channel.pins = AsyncMock(return_value=[panel_msg])

        mock_factory = _mock_async_session()
        with (
            patch("src.ui.control_panel.async_session", mock_factory),
            patch(
                "src.ui.control_panel.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            await refresh_panel_embed(channel)

            panel_msg.edit.assert_awaited_once()
            call_kwargs = panel_msg.edit.call_args[1]
            assert "embed" in call_kwargs
            assert "view" in call_kwargs

    async def test_no_session_does_nothing(self) -> None:
        """セッションが見つからない場合は何もしない。"""
        channel = _make_channel()

        mock_factory = _mock_async_session()
        with (
            patch("src.ui.control_panel.async_session", mock_factory),
            patch(
                "src.ui.control_panel.get_voice_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await refresh_panel_embed(channel)

            channel.pins.assert_not_awaited()

    async def test_no_owner_does_nothing(self) -> None:
        """オーナーが見つからない場合は何もしない。"""
        channel = _make_channel(owner=None)
        voice_session = _make_voice_session()

        mock_factory = _mock_async_session()
        with (
            patch("src.ui.control_panel.async_session", mock_factory),
            patch(
                "src.ui.control_panel.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            await refresh_panel_embed(channel)

            channel.pins.assert_not_awaited()

    async def test_no_panel_in_pins(self) -> None:
        """ピンにパネルメッセージがない場合は何もしない。"""
        owner = MagicMock(spec=discord.Member)
        owner.mention = "<@1>"
        channel = _make_channel(owner=owner)
        voice_session = _make_voice_session()

        # 関係ないメッセージのみ
        other_msg = MagicMock()
        other_msg.author = MagicMock()  # Bot ではない
        other_msg.embeds = []
        channel.pins = AsyncMock(return_value=[other_msg])

        mock_factory = _mock_async_session()
        with (
            patch("src.ui.control_panel.async_session", mock_factory),
            patch(
                "src.ui.control_panel.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            await refresh_panel_embed(channel)

            # edit は呼ばれない
            other_msg.edit.assert_not_called()

    async def test_skips_non_bot_messages(self) -> None:
        """Bot 以外のメッセージはスキップされる。"""
        owner = MagicMock(spec=discord.Member)
        owner.mention = "<@1>"
        channel = _make_channel(owner=owner)
        voice_session = _make_voice_session()

        # 別ユーザーの Embed 付きメッセージ
        user_msg = MagicMock()
        user_msg.author = MagicMock()  # guild.me とは別
        embed = MagicMock()
        embed.title = "ボイスチャンネル設定"
        user_msg.embeds = [embed]
        channel.pins = AsyncMock(return_value=[user_msg])

        mock_factory = _mock_async_session()
        with (
            patch("src.ui.control_panel.async_session", mock_factory),
            patch(
                "src.ui.control_panel.get_voice_session",
                new_callable=AsyncMock,
                return_value=voice_session,
            ),
        ):
            await refresh_panel_embed(channel)

            user_msg.edit.assert_not_called()
