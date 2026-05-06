"""Discord bot class definition.

Bot 本体のクラス定義。起動時の初期化処理（Cog の読み込み、
永続 View の復元）を担う。

Examples:
    基本的な使い方::

        from src.bot import EphemeralVCBot
        from src.config import settings

        bot = EphemeralVCBot()
        async with bot:
            await bot.start(settings.discord_token)
"""

import logging
import os

import discord
from discord.ext import commands

from src.database.engine import async_session
from src.services.db_service import get_all_voice_sessions
from src.ui.control_panel import ControlPanelView

logger = logging.getLogger(__name__)


class EphemeralVCBot(commands.Bot):
    """一時 VC の Bot 本体。

    discord.py の commands.Bot を継承し、一時ボイスチャンネル機能を提供する。
    """

    def __init__(self) -> None:
        """Bot インスタンスを初期化する。

        Notes:
            設定される Intents:

            - voice_states: ボイスチャンネルの参加/退出イベント
            - guilds: サーバー情報の取得
            - members: メンバー情報の取得 (特権 Intent)
        """
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        intents.members = True

        activity = discord.Game(name="一時 VC を管理しています")

        super().__init__(
            command_prefix="!",
            intents=intents,
            activity=activity,
        )

    async def setup_hook(self) -> None:
        """Bot 起動前に呼ばれるフック。Cog・View の初期化を行う。"""
        # 1. Cog の読み込み
        extensions = [
            "src.cogs.voice",
        ]
        for ext in extensions:
            try:
                await self.load_extension(ext)
                logger.info("Loaded extension: %s", ext)
            except commands.ExtensionError as e:
                logger.exception("Failed to load extension %s: %s", ext, e)
                raise

        # 2. 永続 View の復元
        async with async_session() as session:
            sessions = await get_all_voice_sessions(session)
            logger.info("Restoring %d persistent views from database", len(sessions))
            for voice_session in sessions:
                # NSFW 状態は DB に保存していないため、チャンネルから取得する
                is_nsfw = False
                channel = self.get_channel(int(voice_session.channel_id))
                if channel is None:
                    logger.debug(
                        "Channel %s not in cache for session %d, using NSFW=False",
                        voice_session.channel_id,
                        voice_session.id,
                    )
                elif isinstance(channel, discord.VoiceChannel):
                    is_nsfw = channel.nsfw
                else:
                    logger.warning(
                        "Channel %s is not a VoiceChannel (type=%s) for session %d",
                        voice_session.channel_id,
                        type(channel).__name__,
                        voice_session.id,
                    )
                view = ControlPanelView(
                    voice_session.id,
                    voice_session.is_locked,
                    voice_session.is_hidden,
                    is_nsfw,
                )
                self.add_view(view)

        # 3. スラッシュコマンドの同期
        # SYNC_GUILD_IDS にカンマ区切りでギルド ID を列挙すると、それぞれへ
        # 即時同期する (グローバル同期は Discord 側で最大 1 時間の伝搬遅延が
        # あるため、初回デプロイや開発中はギルド ID を指定するのが推奨)。
        # SYNC_GUILD_ID (単数形) も後方互換として受け付ける。
        sync_guild_ids = (
            os.environ.get("SYNC_GUILD_IDS") or os.environ.get("SYNC_GUILD_ID") or ""
        ).strip()
        try:
            if sync_guild_ids:
                for raw in sync_guild_ids.split(","):
                    gid = raw.strip()
                    if not gid:
                        continue
                    guild = discord.Object(id=int(gid))
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    logger.info(
                        "Synced %d slash commands to guild %s", len(synced), gid
                    )
            else:
                synced = await self.tree.sync()
                logger.info(
                    "Synced %d slash commands globally "
                    "(may take up to 1 hour to propagate)",
                    len(synced),
                )
        except discord.HTTPException as e:
            logger.exception("Failed to sync slash commands: %s", e)
            raise

    async def on_ready(self) -> None:
        """Bot が Discord に接続完了したときに呼ばれる。"""
        if self.user:
            print(f"Logged in as {self.user} (ID: {self.user.id})")
        print("------")
