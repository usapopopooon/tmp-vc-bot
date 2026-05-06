"""Control panel UI components for voice channels.

一時 VC のコントロールパネル UI。
オーナーがチャンネルの設定を変更するためのボタン・モーダル・セレクトメニューを提供する。

UI の構成:
  - ControlPanelView: メインのボタン群 (永続 View)
  - Modal: テキスト入力フォーム (名前変更、人数制限)
  - SelectView: ドロップダウン選択 (譲渡、キック、ブロック、許可等)

discord.py の UI コンポーネント:
  - View: ボタンやセレクトメニューをまとめるコンテナ
  - Button: クリック可能なボタン
  - Modal: ポップアップのテキスト入力フォーム
  - Select: ドロップダウンメニュー
  - interaction.response: ユーザーの操作に対する応答
  - ephemeral=True: 操作者にだけ見えるメッセージ
"""

import asyncio
import contextlib
import logging
import time
from typing import Any

import discord
from discord.ext import commands

from src.constants import DEFAULT_EMBED_COLOR
from src.core.permissions import is_owner
from src.core.validators import validate_channel_name, validate_user_limit
from src.database.engine import async_session
from src.database.models import VoiceSession
from src.services.db_service import (
    delete_voice_session,
    get_voice_session,
    update_voice_session,
)
from src.utils import get_resource_lock

logger = logging.getLogger(__name__)

# =============================================================================
# コントロールパネル操作クールダウン (連打対策)
# =============================================================================

# コントロールパネル操作のクールダウン時間 (秒)
CONTROL_PANEL_COOLDOWN_SECONDS = 3

# ユーザーごとの最終操作時刻を記録
# key: (user_id, channel_id), value: timestamp (float)
_control_panel_cooldown_cache: dict[tuple[int, int], float] = {}

# キャッシュクリーンアップ間隔
_CLEANUP_INTERVAL = 300  # 5分
_last_cleanup_time = 0.0


def _cleanup_control_panel_cooldown_cache() -> None:
    """古いコントロールパネルクールダウンエントリを削除する."""
    global _last_cleanup_time
    now = time.monotonic()

    # 5分ごとにクリーンアップ
    if _last_cleanup_time > 0 and now - _last_cleanup_time < _CLEANUP_INTERVAL:
        return

    _last_cleanup_time = now

    # 1パス削除: キーのスナップショットから期限切れをその場で削除
    for key in list(_control_panel_cooldown_cache):
        if now - _control_panel_cooldown_cache[key] > _CLEANUP_INTERVAL:
            del _control_panel_cooldown_cache[key]


def is_control_panel_on_cooldown(user_id: int, channel_id: int) -> bool:
    """ユーザーがコントロールパネル操作のクールダウン中かどうかを確認する.

    Args:
        user_id: Discord ユーザー ID
        channel_id: チャンネル ID

    Returns:
        クールダウン中なら True
    """
    _cleanup_control_panel_cooldown_cache()

    key = (user_id, channel_id)
    now = time.monotonic()

    last_time = _control_panel_cooldown_cache.get(key)
    if last_time is not None and now - last_time < CONTROL_PANEL_COOLDOWN_SECONDS:
        return True

    # クールダウンを記録/更新
    _control_panel_cooldown_cache[key] = now
    return False


def clear_control_panel_cooldown_cache() -> None:
    """コントロールパネルクールダウンキャッシュをクリアする (テスト用)."""
    global _last_cleanup_time
    _control_panel_cooldown_cache.clear()
    _last_cleanup_time = 0.0


# パネルメッセージの Embed タイトル (検索用定数)
_PANEL_TITLE = "ボイスチャンネル設定"


async def _find_panel_message(
    channel: discord.VoiceChannel,
) -> discord.Message | None:
    """チャンネル内のコントロールパネルメッセージを探す。

    ピン留めメッセージを優先的に検索し、見つからなければ
    チャンネル履歴から Bot の Embed メッセージを探す。
    """
    bot_user = channel.guild.me

    # ピン留めメッセージから探す
    try:
        pins = await channel.pins()
        for msg in pins:
            if (
                msg.author == bot_user
                and msg.embeds
                and msg.embeds[0].title == _PANEL_TITLE
            ):
                return msg
    except discord.HTTPException as e:
        logger.debug("Failed to fetch pins for channel %s: %s", channel.id, e)

    # フォールバック: 履歴から探す (ピン留めされていないパネル)
    try:
        async for hist_msg in channel.history(limit=50):
            if (
                hist_msg.author == bot_user
                and hist_msg.embeds
                and hist_msg.embeds[0].title == _PANEL_TITLE
            ):
                return hist_msg
    except discord.HTTPException as e:
        logger.debug("Failed to fetch history for channel %s: %s", channel.id, e)

    logger.debug("Control panel not found for channel %s", channel.id)
    return None


def create_control_panel_embed(
    session: VoiceSession, owner: discord.Member
) -> discord.Embed:
    """コントロールパネルの Embed (情報表示部分) を作成する。

    チャンネルに送信される情報カードで、オーナー名とチャンネルの状態を表示する。

    Args:
        session: DB の VoiceSession オブジェクト
        owner: チャンネルオーナーの Discord メンバー

    Returns:
        組み立てた Embed オブジェクト
    """
    embed = discord.Embed(
        title="ボイスチャンネル設定",
        # owner.mention → @ユーザー名 のメンション形式 (クリックでプロフィール表示)
        description=f"オーナー: {owner.mention}",
        color=DEFAULT_EMBED_COLOR,
    )

    lock_status = "ロック中" if session.is_locked else "未ロック"
    limit_status = str(session.user_limit) if session.user_limit > 0 else "無制限"

    embed.add_field(name="状態", value=lock_status, inline=True)
    embed.add_field(name="人数制限", value=limit_status, inline=True)

    return embed


async def refresh_panel_embed(
    channel: discord.VoiceChannel,
) -> None:
    """パネルメッセージの Embed を最新の DB 状態で更新する。"""
    async with async_session() as db_session:
        voice_session = await get_voice_session(db_session, str(channel.id))
        if not voice_session:
            logger.debug("No voice session found for channel %s", channel.id)
            return

        owner = channel.guild.get_member(int(voice_session.owner_id))
        if not owner:
            logger.warning(
                "Owner %s not found for channel %s",
                voice_session.owner_id,
                channel.id,
            )
            return

        embed = create_control_panel_embed(voice_session, owner)

        # パネルメッセージを探して更新 (ピン → 履歴の順)
        panel_msg = await _find_panel_message(channel)
        if panel_msg:
            view = ControlPanelView(
                voice_session.id,
                voice_session.is_locked,
                voice_session.is_hidden,
                channel.nsfw,
            )
            try:
                await panel_msg.edit(embed=embed, view=view)
            except discord.HTTPException as e:
                logger.error(
                    "Failed to edit panel message in channel %s: %s",
                    channel.id,
                    e,
                )


async def repost_panel(
    channel: discord.VoiceChannel,
    bot: commands.Bot,
) -> None:
    """旧パネルを削除し、新しいパネルを送信する。

    refresh_panel_embed() が既存メッセージを edit で更新するのに対し、
    この関数はメッセージを削除→再作成する。オーナー譲渡時や /panel コマンドで使用。
    """
    async with async_session() as db_session:
        voice_session = await get_voice_session(db_session, str(channel.id))
        if not voice_session:
            logger.debug("No voice session found for channel %s in repost", channel.id)
            return

        owner = channel.guild.get_member(int(voice_session.owner_id))
        if not owner:
            logger.warning(
                "Owner %s not found for channel %s in repost",
                voice_session.owner_id,
                channel.id,
            )
            return

        # 旧パネル削除 (ピン → 履歴の順で探す)
        old_panel = await _find_panel_message(channel)
        if old_panel:
            try:
                await old_panel.delete()
            except discord.HTTPException as e:
                logger.debug(
                    "Failed to delete old panel in channel %s: %s",
                    channel.id,
                    e,
                )

        # 新パネル送信
        embed = create_control_panel_embed(voice_session, owner)
        view = ControlPanelView(
            voice_session.id,
            voice_session.is_locked,
            voice_session.is_hidden,
            channel.nsfw,
        )
        bot.add_view(view)
        try:
            await channel.send(embed=embed, view=view)
            logger.debug("Reposted panel in channel %s", channel.id)
        except discord.HTTPException as e:
            logger.error(
                "Failed to send new panel in channel %s: %s",
                channel.id,
                e,
            )


# =============================================================================
# Modals (ポップアップ入力フォーム)
# =============================================================================


class RenameModal(discord.ui.Modal, title="チャンネル名変更"):
    """チャンネル名を変更するモーダル (ポップアップ入力フォーム)。

    discord.ui.Modal を継承して作る。
    title= でモーダルのタイトルを設定する。
    """

    # TextInput: テキスト入力フィールド。クラス変数として定義する。
    name: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="新しいチャンネル名",
        placeholder="チャンネル名を入力...",  # 未入力時のヒントテキスト
        min_length=1,
        max_length=100,  # Discord のチャンネル名上限
    )

    def __init__(self, session_id: int, *, current_name: str = "") -> None:
        super().__init__()
        self.session_id = session_id
        if current_name:
            self.name.default = current_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """モーダルの送信ボタンが押されたときの処理。

        1. 入力値のバリデーション
        2. オーナー権限チェック
        3. Discord API でチャンネル名を変更
        4. DB を更新
        """
        new_name = str(self.name.value)

        if not validate_channel_name(new_name):
            await interaction.response.send_message(
                "無効なチャンネル名です。", ephemeral=True
            )
            return

        async with async_session() as db_session:
            voice_session = await get_voice_session(
                db_session, str(interaction.channel_id)
            )
            if not voice_session:
                await interaction.response.send_message(
                    "セッションが見つかりません。", ephemeral=True
                )
                return

            if not is_owner(voice_session.owner_id, interaction.user.id):
                await interaction.response.send_message(
                    "オーナーのみチャンネル名を変更できます。", ephemeral=True
                )
                return

            # Discord API でチャンネル名を変更
            channel = interaction.channel
            if isinstance(channel, discord.VoiceChannel):
                await channel.edit(name=new_name)

            # DB のチャンネル名も更新
            await update_voice_session(db_session, voice_session, name=new_name)

        # チャンネルに変更通知を送信
        channel = interaction.channel
        if isinstance(channel, discord.VoiceChannel):
            await interaction.response.defer()
            await channel.send(f"🏷️ チャンネル名が **{new_name}** に変更されました。")
            await refresh_panel_embed(channel)
        else:
            await interaction.response.defer()


class UserLimitModal(discord.ui.Modal, title="人数制限変更"):
    """人数制限を変更するモーダル。"""

    limit: discord.ui.TextInput[Any] = discord.ui.TextInput(
        label="人数制限 (0〜99、0 = 無制限)",
        placeholder="0〜99の数字を入力...",
        min_length=1,
        max_length=2,
    )

    def __init__(self, session_id: int, *, current_limit: int = 0) -> None:
        super().__init__()
        self.session_id = session_id
        self.limit.default = str(current_limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """モーダル送信時の処理。入力値を数値に変換し、バリデーション後に適用する。"""
        # 文字列 → 数値に変換。数値でなければエラー
        try:
            new_limit = int(self.limit.value)
        except ValueError:
            await interaction.response.send_message(
                "有効な数字を入力してください。", ephemeral=True
            )
            return

        # 0〜99 の範囲チェック
        if not validate_user_limit(new_limit):
            await interaction.response.send_message(
                "無効な人数制限です。0〜99の範囲で入力してください。",
                ephemeral=True,
            )
            return

        async with async_session() as db_session:
            voice_session = await get_voice_session(
                db_session, str(interaction.channel_id)
            )
            if not voice_session:
                await interaction.response.send_message(
                    "セッションが見つかりません。", ephemeral=True
                )
                return

            if not is_owner(voice_session.owner_id, interaction.user.id):
                await interaction.response.send_message(
                    "オーナーのみ人数制限を変更できます。", ephemeral=True
                )
                return

            # Discord API で人数制限を変更
            channel = interaction.channel
            if isinstance(channel, discord.VoiceChannel):
                await channel.edit(user_limit=new_limit)

            # DB を更新
            await update_voice_session(db_session, voice_session, user_limit=new_limit)

        limit_text = str(new_limit) if new_limit > 0 else "無制限"

        # チャンネルに変更通知を送信
        channel = interaction.channel
        if isinstance(channel, discord.VoiceChannel):
            await interaction.response.defer()
            await channel.send(f"👥 人数制限が **{limit_text}** に変更されました。")
            await refresh_panel_embed(channel)
        else:
            await interaction.response.defer()


# =============================================================================
# Ephemeral Select Views (ボタン押下時に表示されるセレクトメニュー)
# =============================================================================
# ephemeral = 操作者にだけ見えるメッセージとして表示される


class TransferSelectView(discord.ui.View):
    """オーナー譲渡先を選択するセレクトメニュー。

    チャンネル内のメンバー一覧をドロップダウンで表示する。
    timeout=60: 60秒操作がないと自動で無効化される。
    """

    def __init__(self, channel: discord.VoiceChannel, owner_id: int) -> None:
        super().__init__(timeout=60)
        # オーナー自身と Bot を除外した候補リストを作成
        members = [m for m in channel.members if m.id != owner_id and not m.bot]
        if not members:
            return  # 誰もいなければセレクトメニューを追加しない
        # SelectOption: ドロップダウンの選択肢 (label=表示名, value=内部値)
        # Discord の制限: セレクトの選択肢は最大25個
        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in members[:25]
        ]
        self.add_item(TransferSelectMenu(options))


class TransferSelectMenu(discord.ui.Select[Any]):
    """オーナー譲渡のセレクトメニュー本体。"""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="新しいオーナーを選択...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        """ユーザーが選択したときの処理。

        1. 選択されたメンバーを取得
        2. テキストチャット権限を移行
        3. DB のオーナー ID を更新
        """
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        guild = interaction.guild
        if not guild:
            return

        # self.values[0]: 選択された値 (ユーザー ID の文字列)
        new_owner = guild.get_member(int(self.values[0]))
        if not new_owner:
            await interaction.response.edit_message(
                content="メンバーが見つかりません。", view=None
            )
            return

        # チャンネルごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(f"control_panel:{channel.id}"):
            async with async_session() as db_session:
                voice_session = await get_voice_session(
                    db_session, str(interaction.channel_id)
                )
                if not voice_session:
                    await interaction.response.edit_message(
                        content="セッションが見つかりません。", view=None
                    )
                    return

                # テキストチャット権限の移行
                # 旧オーナー: read_message_history=None (ロール設定に戻す)
                if isinstance(interaction.user, discord.Member):
                    await channel.set_permissions(
                        interaction.user,
                        read_message_history=None,
                    )
                # 新オーナー: read_message_history=True (閲覧可)
                await channel.set_permissions(new_owner, read_message_history=True)

                # DB のオーナー ID を更新
                await update_voice_session(
                    db_session,
                    voice_session,
                    owner_id=str(new_owner.id),
                )

            # ephemeral のセレクトメニューを削除し、チャンネルに通知
            await interaction.response.edit_message(content="\u200b", view=None)
            old = interaction.user.mention
            new = new_owner.mention
            await channel.send(f"👑 {old} → {new} にオーナーが譲渡されました。")

            # パネルを再投稿 (旧パネル削除 → 新パネル送信 + ピン留め)
            await repost_panel(channel, interaction.client)  # type: ignore[arg-type]


class KickSelectView(discord.ui.View):
    """キック対象を選択するセレクトメニュー。

    チャンネル内のメンバー一覧をドロップダウンで表示する。
    オーナー自身と Bot は選択肢から除外される。
    """

    def __init__(self, channel: discord.VoiceChannel, owner_id: int) -> None:
        super().__init__(timeout=60)
        # オーナー自身と Bot を除外した候補リストを作成
        members = [m for m in channel.members if m.id != owner_id and not m.bot]
        if not members:
            return  # 誰もいなければセレクトメニューを追加しない
        # SelectOption: ドロップダウンの選択肢 (label=表示名, value=内部値)
        # Discord の制限: セレクトの選択肢は最大25個
        options = [
            discord.SelectOption(label=m.display_name, value=str(m.id))
            for m in members[:25]
        ]
        self.add_item(KickSelectMenu(options))


class KickSelectMenu(discord.ui.Select[Any]):
    """キックのセレクトメニュー本体。"""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="キックするユーザーを選択...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        """ユーザー選択時の処理。VC から切断する (move_to(None))。"""
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        guild = interaction.guild
        if not guild:
            return

        # self.values[0]: 選択された値 (ユーザー ID の文字列)
        user_to_kick = guild.get_member(int(self.values[0]))
        if not user_to_kick:
            await interaction.response.edit_message(
                content="メンバーが見つかりません。",
                view=None,
            )
            return

        # move_to(None) でユーザーを VC から切断する
        await user_to_kick.move_to(None)
        # ephemeral のセレクトメニューを削除し、チャンネルに通知
        await interaction.response.edit_message(content="\u200b", view=None)
        await channel.send(f"👟 {user_to_kick.mention} がキックされました。")


class BlockSelectView(discord.ui.View):
    """ブロック対象を選択するユーザーセレクト。

    ブロック = connect=False で接続権限を拒否する。
    既に VC にいる場合はキックもする。
    """

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.owner_id = owner_id

    @discord.ui.select(
        cls=discord.ui.UserSelect, placeholder="ブロックするユーザーを選択..."
    )
    async def select_user(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect[Any]
    ) -> None:
        """ユーザー選択時の処理。接続権限を拒否し、VC にいればキックする。"""
        user_to_block = select.values[0]
        channel = interaction.channel

        if not isinstance(channel, discord.VoiceChannel):
            return

        if not isinstance(user_to_block, discord.Member):
            return

        # オーナー自身はブロックできない
        if user_to_block.id == self.owner_id:
            await interaction.response.edit_message(
                content="自分自身をブロックすることはできません。",
                view=None,
            )
            return

        # connect=False で接続を拒否
        await channel.set_permissions(user_to_block, connect=False)

        # 既に VC にいる場合はキック
        if (
            isinstance(user_to_block, discord.Member)
            and user_to_block.voice
            and user_to_block.voice.channel == channel
        ):
            await user_to_block.move_to(None)

        # ephemeral のセレクトメニューを削除し、チャンネルに通知
        await interaction.response.edit_message(content="\u200b", view=None)
        await channel.send(f"🚫 {user_to_block.mention} がブロックされました。")


class AllowSelectView(discord.ui.View):
    """許可対象を選択するユーザーセレクト。

    許可 = connect=True で接続権限を許可する。
    ロック状態で特定のユーザーだけ入れるようにする場合に使う。
    """

    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.select(
        cls=discord.ui.UserSelect, placeholder="許可するユーザーを選択..."
    )
    async def select_user(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect[Any]
    ) -> None:
        """ユーザー選択時の処理。接続権限を許可する。"""
        user_to_allow = select.values[0]
        channel = interaction.channel

        if not isinstance(channel, discord.VoiceChannel):
            return

        if not isinstance(user_to_allow, discord.Member):
            return

        # connect=True で接続を許可
        await channel.set_permissions(user_to_allow, connect=True)
        # ephemeral のセレクトメニューを削除し、チャンネルに通知
        await interaction.response.edit_message(content="\u200b", view=None)
        await channel.send(f"✅ {user_to_allow.mention} が許可されました。")


class CameraToggleSelectView(discord.ui.View):
    """カメラ権限をトグルするセレクトメニュー。

    チャンネル内のメンバー一覧をドロップダウンで表示する。
    オーナー自身と Bot は選択肢から除外される。
    選択したユーザーのカメラ/配信権限をトグルする。
    """

    def __init__(self, channel: discord.VoiceChannel, owner_id: int) -> None:
        super().__init__(timeout=60)
        self.channel = channel
        # オーナー自身と Bot を除外した候補リストを作成
        members = [m for m in channel.members if m.id != owner_id and not m.bot]
        if not members:
            return  # 誰もいなければセレクトメニューを追加しない

        # 各メンバーの現在のカメラ権限状態を表示
        options = []
        for m in members[:25]:
            overwrites = channel.overwrites_for(m)
            if overwrites.stream is False:
                # 禁止中
                label = f"📵 {m.display_name} (禁止中)"
            else:
                # 許可 (デフォルトまたは明示的許可)
                label = f"📹 {m.display_name}"
            options.append(discord.SelectOption(label=label, value=str(m.id)))
        self.add_item(CameraToggleSelectMenu(options))


class CameraToggleSelectMenu(discord.ui.Select[Any]):
    """カメラ権限トグルのセレクトメニュー本体。"""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(
            placeholder="カメラ権限を切り替えるユーザーを選択...", options=options
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """ユーザー選択時の処理。配信権限をトグルする。"""
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        guild = interaction.guild
        if not guild:
            return

        user = guild.get_member(int(self.values[0]))
        if not user:
            await interaction.response.edit_message(
                content="メンバーが見つかりません。",
                view=None,
            )
            return

        # 現在の権限を取得してトグル
        overwrites = channel.overwrites_for(user)
        if overwrites.stream is False:
            # 禁止中 → 許可 (上書きを削除してデフォルトに戻す)
            await channel.set_permissions(user, stream=None)
            await interaction.response.edit_message(content="\u200b", view=None)
            await channel.send(f"📹 {user.mention} のカメラ配信が許可されました。")
        else:
            # 許可 → 禁止
            await channel.set_permissions(user, stream=False)
            await interaction.response.edit_message(content="\u200b", view=None)
            await channel.send(f"📵 {user.mention} のカメラ配信が禁止されました。")


class BitrateSelectView(discord.ui.View):
    """ビットレートを選択するセレクトメニュー。

    ビットレート = 音声品質。高いほど高音質だが帯域を使う。
    サーバーのブーストレベルで上限が変わる。
    """

    # (表示ラベル, 値) のリスト。値は bps (bits per second) 単位
    BITRATES = [
        ("8 kbps", "8000"),
        ("16 kbps", "16000"),
        ("32 kbps", "32000"),
        ("64 kbps", "64000"),
        ("96 kbps", "96000"),
        ("128 kbps", "128000"),
        ("256 kbps", "256000"),
        ("384 kbps", "384000"),
    ]

    def __init__(self) -> None:
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=label, value=value)
            for label, value in self.BITRATES
        ]
        self.add_item(BitrateSelectMenu(options))


class BitrateSelectMenu(discord.ui.Select[Any]):
    """ビットレートセレクトメニュー本体。"""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="ビットレートを選択...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        """選択時の処理。Discord API でビットレートを変更する。"""
        bitrate = int(self.values[0])  # bps 単位の値
        channel = interaction.channel

        if isinstance(channel, discord.VoiceChannel):
            try:
                await channel.edit(bitrate=bitrate)
            except discord.HTTPException:
                # サーバーのブーストレベルが足りない場合
                await interaction.response.edit_message(
                    content="このサーバーのブーストレベルでは"
                    "利用できないビットレートです。",
                    view=None,
                )
                return

        label = f"{bitrate // 1000} kbps"
        # ephemeral のセレクトメニューを削除し、チャンネルに通知
        await interaction.response.edit_message(content="\u200b", view=None)
        if isinstance(channel, discord.VoiceChannel):
            await channel.send(f"🔊 ビットレートが **{label}** に変更されました。")


class RegionSelectView(discord.ui.View):
    """VC リージョン (サーバー地域) を選択するセレクトメニュー。

    リージョン = 音声サーバーの地理的位置。近い方が低遅延。
    「自動」は Discord が最適なリージョンを選択する。
    """

    # (表示ラベル, Discord API の値) のリスト
    REGIONS = [
        ("自動", "auto"),
        ("日本", "japan"),
        ("シンガポール", "singapore"),
        ("香港", "hongkong"),
        ("シドニー", "sydney"),
        ("インド", "india"),
        ("米国西部", "us-west"),
        ("米国東部", "us-east"),
        ("米国中部", "us-central"),
        ("米国南部", "us-south"),
        ("ヨーロッパ", "europe"),
        ("ブラジル", "brazil"),
        ("南アフリカ", "southafrica"),
        ("ロシア", "russia"),
    ]

    # 値から日本語ラベルへのマッピング
    REGION_LABELS = {value: label for label, value in REGIONS}

    def __init__(self) -> None:
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=label, value=value)
            for label, value in self.REGIONS
        ]
        self.add_item(RegionSelectMenu(options))


class RegionSelectMenu(discord.ui.Select[Any]):
    """リージョンセレクトメニュー本体。"""

    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder="リージョンを選択...", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        """選択時の処理。Discord API でリージョンを変更する。"""
        selected = self.values[0]
        # "auto" の場合は None を渡す (Discord が自動選択)
        region = None if selected == "auto" else selected
        channel = interaction.channel

        if isinstance(channel, discord.VoiceChannel):
            await channel.edit(rtc_region=region)

        region_name = RegionSelectView.REGION_LABELS.get(selected, selected)
        # ephemeral のセレクトメニューを削除し、チャンネルに通知
        await interaction.response.edit_message(content="\u200b", view=None)
        if isinstance(channel, discord.VoiceChannel):
            await channel.send(f"🌏 リージョンが **{region_name}** に変更されました。")


class DissolveConfirmView(discord.ui.View):
    """解散の確認ダイアログ。

    「解散する」ボタンで10秒カウントダウン後に全メンバーをキックし、
    チャンネルを削除する。「キャンセル」で操作を取り消す。
    """

    def __init__(self, channel: discord.VoiceChannel) -> None:
        super().__init__(timeout=30)
        self.channel = channel

    @discord.ui.button(
        label="解散する",
        emoji="💣",
        style=discord.ButtonStyle.danger,
    )
    async def confirm_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """解散を実行する。カウントダウン後に全メンバーをキックしチャンネルを削除。"""
        # ephemeral の確認ダイアログを閉じる
        await interaction.response.edit_message(
            content="解散カウントダウンを開始しました。", view=None
        )

        # チャンネルにカウントダウンメッセージを送信
        cancel_view = DissolveCancelView()
        countdown_msg = await self.channel.send(
            "💣 **10** 秒後にチャンネルを解散します...", view=cancel_view
        )

        # 10秒カウントダウン
        for remaining in range(9, 0, -1):
            await asyncio.sleep(1)
            if cancel_view.cancelled:
                return
            with contextlib.suppress(discord.HTTPException):
                await countdown_msg.edit(
                    content=f"💣 **{remaining}** 秒後にチャンネルを解散します..."
                )

        await asyncio.sleep(1)
        if cancel_view.cancelled:
            return

        # カウントダウン完了 → 解散実行
        cancel_view.stop()
        with contextlib.suppress(discord.HTTPException):
            await countdown_msg.edit(
                content="💣 チャンネルを解散しています...", view=None
            )

        # DB からセッションを先に削除
        # (キック後に voice cog の _handle_channel_leave が発火しても
        #  セッションが見つからず何もしない)
        async with async_session() as db_session:
            await delete_voice_session(db_session, str(self.channel.id))

        # チャンネルを削除 (メンバーは自動的に切断される)
        try:
            await self.channel.delete(reason="オーナーによる解散")
        except discord.NotFound:
            pass  # 既に削除済み
        except discord.HTTPException as e:
            logger.warning("Failed to delete channel %s: %s", self.channel.id, e)

    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """解散をキャンセルする。"""
        await interaction.response.edit_message(
            content="解散をキャンセルしました。", view=None
        )


class DissolveCancelView(discord.ui.View):
    """解散カウントダウン中のキャンセルボタン。"""

    def __init__(self) -> None:
        super().__init__(timeout=15)
        self.cancelled = False

    @discord.ui.button(
        label="キャンセル",
        emoji="✋",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """カウントダウンをキャンセルする。"""
        self.cancelled = True
        self.stop()
        await interaction.response.edit_message(
            content="✋ 解散がキャンセルされました。", view=None
        )


# =============================================================================
# Main Control Panel View (メインのボタン群)
# =============================================================================


class ControlPanelView(discord.ui.View):
    """一時 VC のコントロールパネル。ボタンを5行に配置する。

    discord.py の View は最大5行 (row=0〜4)、各行最大5個のボタンを配置できる。

    ボタン配置:
      Row 0: [名前変更] [人数制限]
      Row 1: [ビットレート] [リージョン]
      Row 2: [ロック] [非表示] [年齢制限]
      Row 3: [譲渡] [キック] [解散]
      Row 4: [ブロック] [許可] [カメラ禁止] [カメラ許可]

    timeout=None: タイムアウトなし (永続 View)。
    custom_id: Bot 再起動後もボタンを識別するための固定 ID。
    """

    def __init__(
        self,
        session_id: int,
        is_locked: bool = False,
        is_hidden: bool = False,
        is_nsfw: bool = False,
    ) -> None:
        # timeout=None で永続 View にする (タイムアウトしない)
        super().__init__(timeout=None)
        self.session_id = session_id

        # 現在の状態に応じてボタンのラベルと絵文字を切り替える
        if is_locked:
            self.lock_button.label = "解除"
            self.lock_button.emoji = "🔓"

        if is_hidden:
            self.hide_button.label = "表示"
            self.hide_button.emoji = "👁️"

        if is_nsfw:
            self.nsfw_button.label = "制限解除"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """全ボタン共通の権限チェック。オーナーのみ操作可能。

        discord.py が各ボタンのコールバック前に自動で呼ぶ。
        False を返すとコールバックが実行されない。
        """
        # クールダウンチェック (連打対策)
        if interaction.channel_id and is_control_panel_on_cooldown(
            interaction.user.id, interaction.channel_id
        ):
            await interaction.response.send_message(
                "操作が早すぎます。少し待ってから再度お試しください。",
                ephemeral=True,
            )
            return False

        async with async_session() as db_session:
            voice_session = await get_voice_session(
                db_session, str(interaction.channel_id)
            )
            if not voice_session:
                await interaction.response.send_message(
                    "セッションが見つかりません。", ephemeral=True
                )
                return False

            if not is_owner(voice_session.owner_id, interaction.user.id):
                await interaction.response.send_message(
                    "チャンネルオーナーのみ操作できます。",
                    ephemeral=True,
                )
                return False

        return True

    # =========================================================================
    # Row 0: チャンネル設定 (名前変更・人数制限)
    # =========================================================================

    @discord.ui.button(
        label="名前変更",
        emoji="🏷️",
        style=discord.ButtonStyle.secondary,  # グレーのボタン
        custom_id="rename_button",  # 永続化用の固定 ID
        row=0,
    )
    async def rename_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """名前変更ボタン。モーダル (入力フォーム) を表示する。"""
        current_name = ""
        channel = interaction.channel
        if isinstance(channel, discord.VoiceChannel):
            current_name = channel.name
        await interaction.response.send_modal(
            RenameModal(self.session_id, current_name=current_name)
        )

    @discord.ui.button(
        label="人数制限",
        emoji="👥",
        style=discord.ButtonStyle.secondary,
        custom_id="limit_button",
        row=0,
    )
    async def limit_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """人数制限ボタン。モーダルを表示する。"""
        current_limit = 0
        channel = interaction.channel
        if isinstance(channel, discord.VoiceChannel):
            current_limit = channel.user_limit
        await interaction.response.send_modal(
            UserLimitModal(self.session_id, current_limit=current_limit)
        )

    # =========================================================================
    # Row 1: チャンネル設定 (ビットレート・リージョン)
    # =========================================================================

    @discord.ui.button(
        label="ビットレート",
        emoji="🔊",
        style=discord.ButtonStyle.secondary,
        custom_id="bitrate_button",
        row=1,
    )
    async def bitrate_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """ビットレートボタン。セレクトメニューを表示する。"""
        await interaction.response.send_message(
            "ビットレートを選択:",
            view=BitrateSelectView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="リージョン",
        emoji="🌏",
        style=discord.ButtonStyle.secondary,
        custom_id="region_button",
        row=1,
    )
    async def region_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """リージョンボタン。セレクトメニューを表示する。"""
        await interaction.response.send_message(
            "リージョンを選択:", view=RegionSelectView(), ephemeral=True
        )

    # =========================================================================
    # Row 2: 状態トグル (ロック・非表示・年齢制限)
    # =========================================================================

    @discord.ui.button(
        label="ロック",
        emoji="🔒",
        style=discord.ButtonStyle.secondary,
        custom_id="lock_button",
        row=2,
    )
    async def lock_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        """ロック/解除トグルボタン。

        ロック時: @everyone と全ロールの connect を拒否、オーナーにフル権限を付与
        解除時: ロック時に追加した権限上書きを削除
        """
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel) or not interaction.guild:
            return

        # レート制限による待機でタイムアウトしないよう、最初に応答
        await interaction.response.defer()

        # チャンネルごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(f"control_panel:{channel.id}"):
            async with async_session() as db_session:
                voice_session = await get_voice_session(
                    db_session, str(interaction.channel_id)
                )
                if not voice_session:
                    return

                # トグル: 現在の状態を反転
                # リソースロックにより、並行リクエストによる lost update を防止
                new_locked_state = not voice_session.is_locked
                name_edit_failed = False
                owner_id = int(voice_session.owner_id)

                if new_locked_state:
                    # ロック: @everyone の接続を拒否
                    await channel.set_permissions(
                        interaction.guild.default_role, connect=False
                    )
                    # チャンネルに設定されている全ロールの connect も拒否
                    # ロールに connect=True があると @everyone の拒否が上書きされる
                    default_role = interaction.guild.default_role
                    for target in channel.overwrites:
                        if isinstance(target, discord.Role) and target != default_role:
                            await channel.set_permissions(target, connect=False)
                    # オーナーにフル権限を付与
                    owner = interaction.guild.get_member(owner_id)
                    if owner:
                        await channel.set_permissions(
                            owner,
                            connect=True,
                            speak=True,
                            stream=True,
                            move_members=True,
                            mute_members=True,
                            deafen_members=True,
                        )
                    # チャンネル名の先頭に🔒を追加 (まだない場合のみ)
                    if not channel.name.startswith("🔒"):
                        try:
                            await channel.edit(name=f"🔒{channel.name}")
                        except discord.HTTPException as e:
                            logger.warning(
                                "Failed to add lock emoji to channel %s: %s",
                                channel.id,
                                e,
                            )
                            name_edit_failed = True
                    # ボタンの表示を「解除」に変更
                    button.label = "解除"
                    button.emoji = "🔓"
                else:
                    # 解除: ロールの connect 拒否を削除
                    default_role = interaction.guild.default_role
                    for target, overwrite in list(channel.overwrites.items()):
                        if not isinstance(target, discord.Role):
                            continue
                        if target == default_role:
                            continue
                        if overwrite.connect is False:
                            # connect の上書きだけ削除 (他の権限は維持)
                            await channel.set_permissions(target, connect=None)
                    # @everyone の権限上書きを削除 (デフォルトに戻す)
                    # overwrite=None で上書きごと削除
                    await channel.set_permissions(
                        interaction.guild.default_role, overwrite=None
                    )
                    # オーナーの特別権限も削除 (通常ユーザーに戻す)
                    owner = interaction.guild.get_member(owner_id)
                    if owner:
                        await channel.set_permissions(owner, overwrite=None)
                    # チャンネル名の先頭から🔒を削除 (ある場合のみ)
                    if channel.name.startswith("🔒"):
                        try:
                            await channel.edit(name=channel.name[1:])
                        except discord.HTTPException as e:
                            logger.warning(
                                "Failed to remove lock emoji from channel %s: %s",
                                channel.id,
                                e,
                            )
                            name_edit_failed = True
                    button.label = "ロック"
                    button.emoji = "🔒"

                # DB を更新
                await update_voice_session(
                    db_session, voice_session, is_locked=new_locked_state
                )

                # Embed を更新
                owner = interaction.guild.get_member(owner_id)
                if owner:
                    embed = create_control_panel_embed(voice_session, owner)
                else:
                    embed = None

            status = "ロック" if new_locked_state else "ロック解除"
            emoji = "🔒" if new_locked_state else "🔓"
            # チャンネルに変更通知を送信
            if name_edit_failed:
                # 名前変更が失敗した場合は通知
                if new_locked_state:
                    hint = "🔒マークは手動で追加してください。"
                else:
                    hint = "🔒マークは手動で削除してください。"
                await channel.send(
                    f"{emoji} チャンネルが **{status}** されました。\n"
                    f"⚠️ チャンネル名の変更が制限されています。{hint}"
                )
            else:
                await channel.send(f"{emoji} チャンネルが **{status}** されました。")
            # defer() を完了させるために edit_original_response を呼ぶ
            if embed:
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.edit_original_response(view=self)

    @discord.ui.button(
        label="非表示",
        emoji="🙈",
        style=discord.ButtonStyle.secondary,
        custom_id="hide_button",
        row=2,
    )
    async def hide_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        """非表示/表示トグルボタン。

        非表示時: @everyone の view_channel を拒否、現在のメンバーには許可
        表示時: @everyone の view_channel 上書きを削除
        """
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel) or not interaction.guild:
            return

        # レート制限による待機でタイムアウトしないよう、最初に応答
        await interaction.response.defer()

        # チャンネルごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(f"control_panel:{channel.id}"):
            async with async_session() as db_session:
                voice_session = await get_voice_session(
                    db_session, str(interaction.channel_id)
                )
                if not voice_session:
                    return

                # リソースロックにより、並行リクエストによる lost update を防止
                new_hidden_state = not voice_session.is_hidden

                if new_hidden_state:
                    # 非表示: @everyone のチャンネル表示を拒否
                    await channel.set_permissions(
                        interaction.guild.default_role, view_channel=False
                    )
                    # 現在チャンネルにいるメンバーには表示を許可
                    for member in channel.members:
                        await channel.set_permissions(member, view_channel=True)
                    button.label = "表示"
                    button.emoji = "👁️"
                else:
                    # 表示: view_channel の上書きを削除
                    # view_channel=None で「上書きなし」にする (ロールの設定に従う)
                    await channel.set_permissions(
                        interaction.guild.default_role, view_channel=None
                    )
                    button.label = "非表示"
                    button.emoji = "🙈"

                await update_voice_session(
                    db_session, voice_session, is_hidden=new_hidden_state
                )

                # Embed を更新
                owner = interaction.guild.get_member(int(voice_session.owner_id))
                if owner:
                    embed = create_control_panel_embed(voice_session, owner)
                else:
                    embed = None

            status = "非表示" if new_hidden_state else "表示"
            emoji = "🙈" if new_hidden_state else "👁️"
            # チャンネルに変更通知を送信
            await channel.send(f"{emoji} チャンネルが **{status}** になりました。")
            # defer() を完了させるために edit_original_response を呼ぶ
            if embed:
                await interaction.edit_original_response(embed=embed, view=self)
            else:
                await interaction.edit_original_response(view=self)

    @discord.ui.button(
        label="年齢制限",
        emoji="🔞",
        style=discord.ButtonStyle.secondary,
        custom_id="nsfw_button",
        row=2,
    )
    async def nsfw_button(
        self, interaction: discord.Interaction, button: discord.ui.Button[Any]
    ) -> None:
        """年齢制限 (NSFW) トグルボタン。

        Discord の NSFW フラグをトグルする。
        NSFW チャンネルでは年齢確認が必要になる。
        """
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel) or not interaction.guild:
            return

        # レート制限による待機でタイムアウトしないよう、最初に応答
        await interaction.response.defer()

        # 現在の NSFW 状態を反転
        new_nsfw = not channel.nsfw

        # Discord API で NSFW フラグを変更
        await channel.edit(nsfw=new_nsfw)

        if new_nsfw:
            button.label = "制限解除"
        else:
            button.label = "年齢制限"

        # Embed を更新するために DB からセッション情報を取得
        async with async_session() as db_session:
            voice_session = await get_voice_session(
                db_session, str(interaction.channel_id)
            )
            if voice_session:
                owner = interaction.guild.get_member(int(voice_session.owner_id))
                if owner:
                    embed = create_control_panel_embed(voice_session, owner)
                else:
                    embed = None
            else:
                embed = None

        status = "年齢制限を設定" if new_nsfw else "年齢制限を解除"
        # チャンネルに変更通知を送信
        await channel.send(f"🔞 チャンネルの **{status}** されました。")
        # defer() を完了させるために edit_original_response を呼ぶ
        if embed:
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.edit_original_response(view=self)

    # =========================================================================
    # Row 3: メンバー管理 (譲渡・キック)
    # =========================================================================

    @discord.ui.button(
        label="譲渡",
        emoji="👑",
        style=discord.ButtonStyle.secondary,
        custom_id="transfer_button",
        row=3,
    )
    async def transfer_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """オーナー譲渡ボタン。メンバー選択セレクトを表示する。"""
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        # 譲渡先候補のセレクトメニューを作成
        view = TransferSelectView(channel, interaction.user.id)
        if not view.children:
            # children が空 = メンバーがいない (セレクトが追加されなかった)
            await interaction.response.send_message(
                "他にメンバーがいません。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "新しいオーナーを選択:", view=view, ephemeral=True
        )

    @discord.ui.button(
        label="キック",
        emoji="👟",
        style=discord.ButtonStyle.secondary,
        custom_id="kick_button",
        row=3,
    )
    async def kick_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """キックボタン。ユーザー選択セレクトを表示する。"""
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        view = KickSelectView(channel, interaction.user.id)
        if not view.children:
            await interaction.response.send_message(
                "キックできるメンバーがいません。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "キックするユーザーを選択:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="解散",
        emoji="💣",
        style=discord.ButtonStyle.danger,
        custom_id="dissolve_button",
        row=3,
    )
    async def dissolve_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """解散ボタン。確認後、全メンバーをキックしてチャンネルを削除する。"""
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel) or not interaction.guild:
            return

        # 確認ダイアログを表示
        await interaction.response.send_message(
            "本当にこのチャンネルを解散しますか？\n"
            "全メンバーがキックされ、チャンネルが削除されます。",
            view=DissolveConfirmView(channel),
            ephemeral=True,
        )

    # =========================================================================
    # Row 4: メンバー管理 (ブロック・許可)
    # =========================================================================

    @discord.ui.button(
        label="ブロック",
        emoji="🚫",
        style=discord.ButtonStyle.secondary,
        custom_id="block_button",
        row=4,
    )
    async def block_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """ブロックボタン。ユーザー選択セレクトを表示する。"""
        await interaction.response.send_message(
            "ブロックするユーザーを選択:",
            view=BlockSelectView(interaction.user.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="許可",
        emoji="✅",
        style=discord.ButtonStyle.secondary,
        custom_id="allow_button",
        row=4,
    )
    async def allow_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """許可ボタン。ユーザー選択セレクトを表示する。"""
        await interaction.response.send_message(
            "許可するユーザーを選択:", view=AllowSelectView(), ephemeral=True
        )

    @discord.ui.button(
        label="カメラ",
        emoji="📹",
        style=discord.ButtonStyle.secondary,
        custom_id="camera_button",
        row=4,
    )
    async def camera_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button[Any]
    ) -> None:
        """カメラ権限トグルボタン。ユーザー選択セレクトを表示する。"""
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        view = CameraToggleSelectView(channel, interaction.user.id)
        if not view.children:
            await interaction.response.send_message(
                "他にメンバーがいません。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "カメラ権限を切り替えるユーザーを選択:",
            view=view,
            ephemeral=True,
        )
