"""Tests for bot lifecycle behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.bot import EphemeralVCBot


def _mock_async_session() -> tuple[MagicMock, AsyncMock]:
    mock_session = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_factory, mock_session


class TestOnReadyHiddenVisibility:
    """Tests for reconciliation of existing hidden voice channels."""

    async def test_reconciles_hidden_session_with_matching_channel(self) -> None:
        """デプロイ前から非表示の VC にも修正済み権限を再適用する。"""
        bot = MagicMock(spec=EphemeralVCBot)
        bot.change_presence = AsyncMock()
        bot.user = None
        channel = MagicMock(spec=discord.VoiceChannel)
        channel.id = 100
        bot.get_channel.return_value = channel
        voice_session = MagicMock()
        voice_session.channel_id = "100"
        voice_session.is_hidden = True
        mock_factory, mock_session = _mock_async_session()

        with (
            patch("src.bot.async_session", mock_factory),
            patch(
                "src.bot.get_all_voice_sessions",
                new_callable=AsyncMock,
                return_value=[voice_session],
            ),
            patch(
                "src.bot.hide_voice_channel",
                new_callable=AsyncMock,
            ) as mock_hide,
        ):
            await EphemeralVCBot.on_ready(bot)

        mock_hide.assert_awaited_once_with(channel, voice_session)
        mock_session.commit.assert_awaited_once()

    async def test_skips_visible_session(self) -> None:
        """表示中の VC の権限には起動時処理で触れない。"""
        bot = MagicMock(spec=EphemeralVCBot)
        bot.change_presence = AsyncMock()
        bot.user = None
        voice_session = MagicMock()
        voice_session.channel_id = "100"
        voice_session.is_hidden = False
        mock_factory, mock_session = _mock_async_session()

        with (
            patch("src.bot.async_session", mock_factory),
            patch(
                "src.bot.get_all_voice_sessions",
                new_callable=AsyncMock,
                return_value=[voice_session],
            ),
            patch(
                "src.bot.hide_voice_channel",
                new_callable=AsyncMock,
            ) as mock_hide,
        ):
            await EphemeralVCBot.on_ready(bot)

        mock_hide.assert_not_awaited()
        mock_session.commit.assert_not_awaited()
