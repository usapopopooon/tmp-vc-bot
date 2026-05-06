"""Voice channel event handlers.

一時 VC (Ephemeral Voice Channel) のコアロジック。
ユーザーがロビーに参加すると新しい VC を作成し、
全員が退出すると自動削除する。オーナー退出時は自動引き継ぎを行う。

フロー:
  1. ユーザーがロビー VC に参加
  2. 新しい VC を作成し、ユーザーをそこに移動
  3. コントロールパネル (Embed + ボタン) を送信
  4. ユーザーが退出 → 全員いなくなったら VC を削除
  5. オーナーが退出 → 最も長くいるメンバーにオーナーを引き継ぎ
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.engine import async_session
from src.database.models import VoiceSession
from src.services.db_service import (
    add_voice_session_member,
    claim_event,
    create_lobby,
    create_voice_session,
    delete_lobbies_by_guild,
    delete_lobby,
    delete_voice_session,
    delete_voice_sessions_by_guild,
    get_all_lobbies,
    get_lobbies_by_guild,
    get_lobby_by_channel_id,
    get_voice_session,
    get_voice_session_members_ordered,
    remove_voice_session_member,
    update_voice_session,
)
from src.ui.control_panel import (
    ControlPanelView,
    create_control_panel_embed,
    repost_panel,
)
from src.utils import get_resource_lock

# デフォルトの VC リージョン (サーバー地域)。"japan" = 東京リージョン
DEFAULT_RTC_REGION = "japan"

# VC 作成のクールダウン時間 (秒)
VC_CREATE_COOLDOWN_SECONDS = 30

logger = logging.getLogger(__name__)

# ==========================================================================
# VC 作成クールダウン (連続作成防止)
# ==========================================================================

# ユーザーごとの最終 VC 作成時刻を記録
# key: user_id, value: timestamp (float, time.monotonic)
_vc_create_cooldown_cache: dict[int, float] = {}

# キャッシュクリーンアップ間隔
_VC_CLEANUP_INTERVAL = 300  # 5分
_vc_last_cleanup_time = 0.0


def _cleanup_vc_create_cooldown_cache() -> None:
    """古いVC作成クールダウンエントリを削除する."""
    global _vc_last_cleanup_time
    now = time.monotonic()

    # 5分ごとにクリーンアップ
    if _vc_last_cleanup_time > 0 and now - _vc_last_cleanup_time < _VC_CLEANUP_INTERVAL:
        return

    _vc_last_cleanup_time = now

    # 1パス削除: キーのスナップショットから期限切れをその場で削除
    for key in list(_vc_create_cooldown_cache):
        if now - _vc_create_cooldown_cache[key] > _VC_CLEANUP_INTERVAL:
            del _vc_create_cooldown_cache[key]


def is_vc_create_on_cooldown(user_id: int) -> tuple[bool, float]:
    """ユーザーが VC 作成のクールダウン中かどうかを確認する.

    Args:
        user_id: Discord ユーザー ID

    Returns:
        (クールダウン中なら True, 残り秒数)
    """
    _cleanup_vc_create_cooldown_cache()

    now = time.monotonic()

    last_time = _vc_create_cooldown_cache.get(user_id)
    if last_time is not None:
        elapsed = now - last_time
        if elapsed < VC_CREATE_COOLDOWN_SECONDS:
            remaining = VC_CREATE_COOLDOWN_SECONDS - elapsed
            return True, remaining

    return False, 0.0


def record_vc_create_cooldown(user_id: int) -> None:
    """VC 作成のクールダウンを記録する."""
    _vc_create_cooldown_cache[user_id] = time.monotonic()


def clear_vc_create_cooldown_cache() -> None:
    """VC作成クールダウンキャッシュをクリアする (テスト用)."""
    global _vc_last_cleanup_time
    _vc_create_cooldown_cache.clear()
    _vc_last_cleanup_time = 0.0


def _copy_overwrite(
    overwrite: discord.PermissionOverwrite | None,
) -> discord.PermissionOverwrite:
    """PermissionOverwrite を複製する。"""
    if overwrite is None:
        return discord.PermissionOverwrite()
    allow, deny = overwrite.pair()
    return discord.PermissionOverwrite.from_pair(allow, deny)


class VoiceCog(commands.Cog):
    """ボイスチャンネルの作成・削除・オーナー管理を行う Cog。

    Cog = discord.py の機能モジュール。関連するイベントハンドラや
    コマンドをまとめて管理できる。bot.load_extension() で読み込む。
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # --- 参加時刻のメモリキャッシュ ---
        # DB 読み込み頻度を減らすためのキャッシュ。
        # 構造: {チャンネルID: {ユーザーID: 参加時刻(monotonic)}}
        # time.monotonic() はシステム起動からの秒数で、時計の変更に影響されない。
        #
        # 注意: Bot 再起動時にキャッシュは消えるが、DB にも保存されているため
        # _get_longest_member_from_db() で正確な順序を取得できる。
        self._join_times: dict[int, dict[int, float]] = {}
        # ロビーチャンネル ID のインメモリキャッシュ
        # None = 未ロード (フォールスルー), set = ロード済み (キャッシュ使用)
        self._lobby_channel_ids: set[str] | None = None

    # ==========================================================================
    # イベントリスナー
    # ==========================================================================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """ボイスチャンネルの状態変化を監視する。

        discord.py が自動的に呼び出すイベントハンドラ。
        以下の場合に発火する:
          - VC に参加した
          - VC から退出した
          - VC を移動した (退出 + 参加 の2回発火)
          - ミュート/スピーカーオフなどの状態変化

        Args:
            member: 状態が変わったメンバー
            before: 変更前の状態 (before.channel = 以前いたチャンネル)
            after: 変更後の状態 (after.channel = 今いるチャンネル)
        """
        # --- 参加処理 ---
        # after.channel が存在し、かつ before と異なる = 新しいチャンネルに参加した
        if (
            after.channel
            and after.channel != before.channel
            and isinstance(after.channel, discord.VoiceChannel)
        ):
            # ロビーに参加した場合は一時 VC を作成する
            await self._handle_lobby_join(member, after.channel)
            # ロック/人数制限のチェック (違反者はキック)
            if await self._enforce_channel_restrictions(member, after.channel):
                # キックされた場合は以降の処理をスキップ
                return
            # 参加時刻を記録 (キャッシュ + DB)
            self._record_join_cache(after.channel.id, member.id)
            await self._record_join_to_db(after.channel.id, member.id)

        # --- 退出処理 ---
        # before.channel が存在し、かつ after と異なる = チャンネルから退出した
        if (
            before.channel
            and before.channel != after.channel
            and isinstance(before.channel, discord.VoiceChannel)
        ):
            # 参加時刻の記録を削除 (キャッシュ + DB)
            self._remove_join_cache(before.channel.id, member.id)
            await self._remove_join_from_db(before.channel.id, member.id)
            # 一時 VC の退出処理 (空なら削除、オーナー退出なら引き継ぎ)
            await self._handle_channel_leave(member, before.channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Discord 上でチャンネルが削除されたときに呼ばれるリスナー。

        管理者が手動で一時 VC を削除した場合、on_voice_state_update は
        発火しないため、DB にレコードが残ってしまう (孤立レコード)。
        このリスナーで削除されたチャンネルの DB レコードをクリーンアップする。
        """
        if not isinstance(channel, discord.VoiceChannel):
            return
        # メモリキャッシュの参加記録を削除
        self._cleanup_channel_cache(channel.id)
        # DB のレコードをクリーンアップ (存在しなくても安全)
        channel_id_str = str(channel.id)
        async with async_session() as session:
            await delete_voice_session(session, channel_id_str)
            # ロビーとして登録されていた場合、そのレコードも削除
            lobby = await get_lobby_by_channel_id(session, channel_id_str)
            if lobby:
                await delete_lobby(session, lobby.id)
                if self._lobby_channel_ids is not None:
                    self._lobby_channel_ids.discard(channel_id_str)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """ギルドからボットが削除された時に関連する VC データを全て削除する。"""
        guild_id = str(guild.id)

        # メモリキャッシュをクリア
        # (guild のチャンネルIDを特定できないため全体は消さない)
        # 注: ギルドのチャンネルは取得できないため、キャッシュは自然に stale になる

        async with async_session() as session:
            # 先にボイスセッションを削除 (外部キー制約のため)
            vs_count = await delete_voice_sessions_by_guild(session, guild_id)
            # 次にロビーを削除
            lobby_count = await delete_lobbies_by_guild(session, guild_id)

        if vs_count > 0 or lobby_count > 0:
            logger.info(
                "Cleaned up %d voice session(s) and %d lobby/lobbies "
                "for removed guild: guild=%s",
                vs_count,
                lobby_count,
                guild_id,
            )

    # ==========================================================================
    # 参加時刻の追跡ヘルパー
    # ==========================================================================

    def _record_join_cache(self, channel_id: int, user_id: int) -> None:
        """メンバーの参加時刻をメモリキャッシュに記録する。

        setdefault() を使い、既に記録がある場合は上書きしない。
        """
        channel_times = self._join_times.setdefault(channel_id, {})
        channel_times.setdefault(user_id, time.monotonic())

    def _remove_join_cache(self, channel_id: int, user_id: int) -> None:
        """メンバーの参加記録をメモリキャッシュから削除する。"""
        if channel_id in self._join_times:
            self._join_times[channel_id].pop(user_id, None)

    def _cleanup_channel_cache(self, channel_id: int) -> None:
        """チャンネルの全参加記録をメモリキャッシュから削除する。"""
        self._join_times.pop(channel_id, None)

    async def _record_join_to_db(self, channel_id: int, user_id: int) -> None:
        """メンバーの参加時刻を DB に記録する。

        チャンネルが一時 VC の場合のみ記録する。
        """
        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel_id))
            if voice_session:
                await add_voice_session_member(session, voice_session.id, str(user_id))

    async def _remove_join_from_db(self, channel_id: int, user_id: int) -> None:
        """メンバーの参加記録を DB から削除する。

        チャンネルが一時 VC の場合のみ削除する。
        """
        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel_id))
            if voice_session:
                await remove_voice_session_member(
                    session, voice_session.id, str(user_id)
                )

    async def _get_longest_member(
        self,
        session: AsyncSession,
        voice_session: VoiceSession,
        channel: discord.VoiceChannel,
        exclude_id: int,
    ) -> discord.Member | None:
        """チャンネル内で最も長く滞在しているメンバーを取得する。

        DB から参加時刻の順序を取得するため、Bot 再起動後も正確に動作する。
        Bot ユーザーは候補から除外する。

        Args:
            session: DB セッション
            voice_session: 対象の VoiceSession
            channel: 対象のボイスチャンネル
            exclude_id: 除外するユーザー ID (退出するオーナー)

        Returns:
            最も長く滞在しているメンバー。誰もいなければ None
        """
        # DB から参加順にソートされたメンバーリストを取得
        db_members = await get_voice_session_members_ordered(session, voice_session.id)

        # チャンネルに実際にいるメンバーの ID セット (Bot 除外、退出者除外)
        present_ids = {
            m.id for m in channel.members if m.id != exclude_id and not m.bot
        }

        # DB の順序を維持しながら、実際にいるメンバーのみをフィルタ
        for db_member in db_members:
            user_id = int(db_member.user_id)
            if user_id in present_ids:
                # guild.get_member() で discord.Member オブジェクトを取得
                member = channel.guild.get_member(user_id)
                if member:
                    return member

        # DB に記録がない場合のフォールバック (キャッシュを使用)
        records = self._join_times.get(channel.id, {})
        remaining = [m for m in channel.members if m.id != exclude_id and not m.bot]
        if not remaining:
            return None
        remaining.sort(key=lambda m: (records.get(m.id, float("inf")), m.id))
        return remaining[0]

    # ==========================================================================
    # 入室制限の強制
    # ==========================================================================

    async def _enforce_channel_restrictions(
        self, member: discord.Member, channel: discord.VoiceChannel
    ) -> bool:
        """一時 VC のロック/人数制限を強制する。

        「メンバーを移動」権限を持つユーザーは Discord の仕様上、
        connect=False のチャンネルにも入室できてしまう。
        このメソッドでは、Administrator 権限を持たないユーザーが
        制限を回避して入室した場合にキックする。

        Args:
            member: 参加したメンバー
            channel: 参加先のボイスチャンネル

        Returns:
            True: キックした (呼び出し元で後続処理をスキップすべき)
            False: キックしなかった (正常な入室)
        """
        # Bot 自身は除外
        if member.bot:
            return False

        # Administrator 権限を持つユーザーは制限なし
        if member.guild_permissions.administrator:
            return False

        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel.id))
            if not voice_session:
                # 一時 VC ではない (ロビーなど)
                return False

            # オーナーは制限なし
            if str(member.id) == voice_session.owner_id:
                return False

            should_kick = False
            reason = ""

            # --- ロックチェック ---
            if voice_session.is_locked:
                # チャンネル権限で明示的に connect=True が設定されているか確認
                overwrites = channel.overwrites_for(member)
                if overwrites.connect is not True:
                    # 許可されていない → キック
                    should_kick = True
                    reason = "ロックされているため"

            # --- 人数制限チェック ---
            # ロックで既にキック対象なら重複チェック不要
            if not should_kick and voice_session.user_limit > 0:
                # 現在の人数 (参加者本人を含む)
                current_count = len([m for m in channel.members if not m.bot])
                if current_count > voice_session.user_limit:
                    # チャンネル権限で明示的に connect=True が設定されているか確認
                    overwrites = channel.overwrites_for(member)
                    if overwrites.connect is not True:
                        should_kick = True
                        reason = "人数制限を超えているため"

            if should_kick:
                # 重複排除テーブルで重複防止 (マルチインスタンス)
                bucket = int(time.time()) // 5
                event_key = f"vc_kick:{channel.id}:{member.id}:{bucket}"
                if not await claim_event(session, event_key):
                    logger.info(
                        "VC kick already claimed by another instance: %s",
                        event_key,
                    )
                    return False

                logger.info(
                    "Kicking member %s from channel %s: %s",
                    member.id,
                    channel.id,
                    reason,
                )
                # キック実行
                try:
                    await member.move_to(None)
                except discord.HTTPException as e:
                    logger.warning(
                        "Failed to kick member %s from channel %s: %s",
                        member.id,
                        channel.id,
                        e,
                    )
                # チャンネルに通知
                try:
                    await channel.send(f"⚠️ {member.mention} は{reason}入室できません。")
                except discord.HTTPException as e:
                    logger.debug(
                        "Failed to send kick notification to channel %s: %s",
                        channel.id,
                        e,
                    )
                # DM で本人に通知 (失敗しても問題ない)
                with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                    await member.send(
                        f"⚠️ **{channel.name}** は{reason}入室できませんでした。"
                    )
                return True

        return False

    # ==========================================================================
    # ロビー参加処理
    # ==========================================================================

    async def _handle_lobby_join(
        self, member: discord.Member, channel: discord.VoiceChannel
    ) -> None:
        """ロビー VC に参加したメンバーの処理を行う。

        処理の流れ:
          1. 参加したチャンネルがロビーか DB で確認
          2. クールダウンチェック (連続作成防止)
          3. ロビーなら新しい VC を作成
          4. DB にセッション情報を記録
          5. VC 作成時にテキストチャット権限を付与 (オーナーのみ閲覧可)
          6. メンバーを新しい VC に移動
          7. コントロールパネル (Embed + ボタン) を送信

        Note:
          ユーザーごとのリソースロックにより、同一ユーザーの並行リクエストを
          シリアライズする。クールダウンと合わせて二重保護。
        """
        # インメモリキャッシュで高速フィルタリング (DB アクセスゼロ)
        if (
            self._lobby_channel_ids is not None
            and str(channel.id) not in self._lobby_channel_ids
        ):
            return

        # ユーザーごとのロックで並行リクエストをシリアライズ
        async with (
            get_resource_lock(f"vc_create:{member.id}"),
            async_session() as session,
        ):
            # DB からロビー情報を取得。ロビーでなければ何もしない
            lobby = await get_lobby_by_channel_id(session, str(channel.id))
            if not lobby:
                return

            # --- クールダウンチェック (連続 VC 作成防止) ---
            on_cooldown, remaining = is_vc_create_on_cooldown(member.id)
            if on_cooldown:
                logger.info(
                    "VC creation on cooldown for user %s (%.0f seconds remaining)",
                    member.id,
                    remaining,
                )
                # クールダウン中の通知 (DM、失敗しても問題ない)
                with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                    await member.send(
                        f"⏳ VCの作成は{remaining:.0f}秒後に可能になります。"
                    )
                # ロビーから切断
                try:
                    await member.move_to(None)
                except discord.HTTPException as e:
                    logger.warning(
                        "Failed to kick cooldown user %s from lobby %s: %s",
                        member.id,
                        channel.id,
                        e,
                    )
                return

            # 別インスタンスが既にメンバーを移動済みか確認
            if member.voice is None or member.voice.channel != channel:
                logger.info(
                    "Member %s no longer in lobby %s, skipping VC creation",
                    member.id,
                    channel.id,
                )
                return

            # 重複排除テーブルで重複防止 (マルチインスタンス)
            bucket = int(time.time()) // 5
            event_key = f"vc_lobby:{member.id}:{channel.id}:{bucket}"
            if not await claim_event(session, event_key):
                logger.info(
                    "VC lobby join already claimed by another instance: %s",
                    event_key,
                )
                return

            guild = member.guild

            # --- カテゴリの決定 ---
            # ロビーにカテゴリ ID が設定されていればそれを使う。
            # なければロビー自体のカテゴリを使う (同じカテゴリに作成)。
            category = None
            if lobby.category_id:
                category = guild.get_channel(int(lobby.category_id))
                if not isinstance(category, discord.CategoryChannel):
                    category = channel.category
            else:
                category = channel.category

            # --- VC の作成 ---
            # チャンネル名は「ユーザー名's channel」形式
            # ロビーチャンネルの権限設定をコピーして
            # @everyone の接続拒否などを引き継ぐ
            # 高速化のため set_permissions() の追加 API 呼び出しを避け、
            # 作成時の overwrites に read_message_history の設定を織り込む。
            channel_name = f"{member.display_name}'s channel"
            overwrites = dict(channel.overwrites)

            default_ow = _copy_overwrite(overwrites.get(guild.default_role))
            default_ow.update(read_message_history=False)
            overwrites[guild.default_role] = default_ow

            owner_ow = _copy_overwrite(overwrites.get(member))
            owner_ow.update(read_message_history=True)
            overwrites[member] = owner_ow

            new_channel = await guild.create_voice_channel(
                name=channel_name,
                category=category,
                user_limit=lobby.default_user_limit,
                rtc_region=DEFAULT_RTC_REGION,  # リージョンを日本に固定
                overwrites=overwrites,  # ロビー権限 + オーナー閲覧権限
            )

            # --- DB にセッション記録 ---
            # VC 作成に成功したら、DB にセッション情報を保存する。
            # 失敗した場合は作成した VC を削除してクリーンアップ。
            try:
                voice_session = await create_voice_session(
                    session,
                    lobby_id=lobby.id,
                    channel_id=str(new_channel.id),
                    owner_id=str(member.id),
                    name=channel_name,
                    user_limit=lobby.default_user_limit,
                )
                # オーナーを最初のメンバーとして DB に登録
                await add_voice_session_member(
                    session, voice_session.id, str(member.id)
                )
                # VC 作成成功後、クールダウンを記録
                record_vc_create_cooldown(member.id)
            except Exception:
                await new_channel.delete()
                raise

            # --- チャンネル初期化 ---
            # DB セッション作成後の全操作をまとめてエラーハンドリングする。
            # move_to, send のいずれかが失敗した場合、
            # 不完全なチャンネルと DB レコードを両方クリーンアップする。
            try:
                # ロビーにいる人間メンバーを一括移動する。
                # スナップショットを作り、移動中の members 変化の影響を避ける。
                lobby_members = [
                    m
                    for m in list(channel.members)
                    if not m.bot and m.voice and m.voice.channel == channel
                ]
                # キャッシュ更新タイミングなどで members に載らないケースに備え、
                # トリガーした本人は必ず移動対象に含める。
                if (
                    member not in lobby_members
                    and member.voice
                    and member.voice.channel == channel
                ):
                    lobby_members.append(member)

                # 並列移動で待ち時間を短縮
                move_results = await asyncio.gather(
                    *(m.move_to(new_channel) for m in lobby_members),
                    return_exceptions=True,
                )
                move_errors = [
                    err
                    for err in move_results
                    if isinstance(err, discord.HTTPException)
                ]
                if move_errors:
                    logger.warning(
                        "Some members failed to move to channel %s: %d/%d failed",
                        new_channel.id,
                        len(move_errors),
                        len(lobby_members),
                    )
                    # オーナー移動に失敗した場合は初期化失敗として扱い、
                    # 既存挙動どおりチャンネル/DBをクリーンアップする。
                    owner_idx = lobby_members.index(member)
                    owner_result = move_results[owner_idx]
                    if isinstance(owner_result, discord.HTTPException):
                        raise owner_result

                # コントロールパネル (Embed + ボタン) を送信
                embed = create_control_panel_embed(voice_session, member)
                view = ControlPanelView(
                    voice_session.id,
                    voice_session.is_locked,
                    voice_session.is_hidden,
                )
                self.bot.add_view(view)
                panel_msg = await new_channel.send(embed=embed, view=view)

                # コントロールパネルをピン留めする。
                # _transfer_ownership で pins() から確実に見つけられるようにする。
                try:
                    await panel_msg.pin()
                except discord.HTTPException as e:
                    logger.debug(
                        "Failed to pin control panel in channel %s: %s",
                        new_channel.id,
                        e,
                    )

                logger.info(
                    "Created ephemeral VC %s for member %s from lobby %s",
                    new_channel.id,
                    member.id,
                    channel.id,
                )

            except discord.HTTPException as e:
                # いずれかの Discord API 呼び出しが失敗した場合、
                # チャンネルと DB レコードを両方削除してクリーンアップ
                logger.error(
                    "Failed to initialize channel %s for member %s: %s",
                    new_channel.id,
                    member.id,
                    e,
                )
                try:
                    await new_channel.delete()
                except discord.HTTPException as del_e:
                    logger.warning(
                        "Failed to cleanup channel %s after error: %s",
                        new_channel.id,
                        del_e,
                    )
                await delete_voice_session(session, str(new_channel.id))
                return

    # ==========================================================================
    # 退出処理
    # ==========================================================================

    async def _handle_channel_leave(
        self, member: discord.Member, channel: discord.VoiceChannel
    ) -> None:
        """一時 VC からメンバーが退出したときの処理。

        処理の流れ:
          1. DB でこのチャンネルが一時 VC か確認
          2. チャンネルが空なら削除
          3. オーナーが退出した場合は最も長くいるメンバーに引き継ぎ
        """
        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel.id))
            if not voice_session:
                return  # 一時 VC ではない (ロビー等) → 何もしない

            # --- 全員退出 → チャンネル削除 ---
            if len(channel.members) == 0:
                # 重複排除テーブルで重複防止 (マルチインスタンス)
                event_key = f"vc_delete:{channel.id}"
                if not await claim_event(session, event_key):
                    logger.info(
                        "VC delete already claimed by another instance: %s",
                        event_key,
                    )
                    return

                logger.info(
                    "Deleting empty ephemeral VC %s (last member: %s)",
                    channel.id,
                    member.id,
                )
                # 参加記録をクリーンアップ (キャッシュのみ。DB は CASCADE で自動削除)
                self._cleanup_channel_cache(channel.id)
                # チャンネルを削除
                try:
                    await channel.delete(reason="Ephemeral VC: All members left")
                except discord.HTTPException as e:
                    logger.warning(
                        "Failed to delete empty channel %s: %s",
                        channel.id,
                        e,
                    )
                # DB からセッション記録を削除
                await delete_voice_session(session, str(channel.id))
                return

            # --- オーナー退出 → 引き継ぎ ---
            if voice_session.owner_id == str(member.id):
                await self._transfer_ownership(session, voice_session, member, channel)

    async def _find_panel_message(
        self, channel: discord.VoiceChannel
    ) -> discord.Message | None:
        """コントロールパネルのメッセージを探す。

        ピン留めメッセージを優先的に検索し、見つからなければ
        チャンネル履歴から Bot の Embed メッセージを探す。

        Returns:
            見つかったメッセージ。見つからなければ None
        """
        # ピン留めメッセージから探す (通常はここで見つかる)
        try:
            pins = await channel.pins()
            for pinned in pins:
                if pinned.author == self.bot.user and pinned.embeds:
                    return pinned
        except discord.HTTPException as e:
            logger.debug(
                "Failed to fetch pins for channel %s: %s",
                channel.id,
                e,
            )

        # フォールバック: 履歴から探す (ピン留め前の古いセッション等)
        try:
            async for hist_msg in channel.history(limit=50):
                if hist_msg.author == self.bot.user and hist_msg.embeds:
                    return hist_msg
        except discord.HTTPException as e:
            logger.debug(
                "Failed to fetch history for channel %s: %s",
                channel.id,
                e,
            )

        logger.warning("Control panel not found for channel %s", channel.id)
        return None

    async def _transfer_ownership(
        self,
        session: AsyncSession,
        voice_session: VoiceSession,
        old_owner: discord.Member,
        channel: discord.VoiceChannel,
    ) -> None:
        """オーナー権限を最も長く滞在しているメンバーに引き継ぐ。

        以下を行う:
          1. 引き継ぎ先メンバーを特定 (Bot は除外)
          2. DB のオーナー ID を更新
          3. テキストチャット権限を移行
          4. コントロールパネルの Embed を更新
          5. チャンネルに通知メッセージを送信
        """
        # 最も長く滞在しているメンバーを取得 (Bot は除外)
        # DB から参加時刻順を取得するため、再起動後も正確
        new_owner = await self._get_longest_member(
            session, voice_session, channel, old_owner.id
        )
        if not new_owner:
            logger.debug(
                "No eligible member for ownership transfer in channel %s",
                channel.id,
            )
            return  # 人間のメンバーが誰もいない

        # 重複排除テーブルで重複防止 (マルチインスタンス)
        bucket = int(time.time()) // 5
        event_key = f"vc_transfer:{channel.id}:{old_owner.id}:{bucket}"
        if not await claim_event(session, event_key):
            logger.info(
                "VC transfer already claimed by another instance: %s",
                event_key,
            )
            return

        logger.info(
            "Transferring ownership of channel %s from %s to %s",
            channel.id,
            old_owner.id,
            new_owner.id,
        )

        # DB のオーナー ID を新オーナーに更新
        await update_voice_session(session, voice_session, owner_id=str(new_owner.id))

        # テキストチャット権限を移行
        # 旧オーナー: read_message_history=None → ロール設定に戻す (= 読めなくなる)
        # 新オーナー: read_message_history=True → 読めるようにする
        try:
            await channel.set_permissions(old_owner, read_message_history=None)
            await channel.set_permissions(new_owner, read_message_history=True)
        except discord.HTTPException as e:
            logger.warning(
                "Failed to update permissions for ownership transfer in channel %s: %s",
                channel.id,
                e,
            )

        # コントロールパネルを再投稿 (旧パネル削除 → 新パネル送信 → ピン留め)
        await repost_panel(channel, self.bot)

        # チャンネルに引き継ぎ通知を送信
        try:
            await channel.send(
                f"オーナーが退出したため、{new_owner.mention} に引き継ぎました。"
            )
        except discord.HTTPException as e:
            logger.debug(
                "Failed to send ownership transfer notification in channel %s: %s",
                channel.id,
                e,
            )

    # ==========================================================================
    # スラッシュコマンド (/vc グループ)
    # ==========================================================================

    vc_group = app_commands.Group(
        name="vc",
        description="一時 VC の管理コマンド",
    )

    @vc_group.command(name="lobby", description="ロビーVCを作成します")
    @app_commands.default_permissions(administrator=True)
    async def vc_lobby(self, interaction: discord.Interaction) -> None:
        """ロビー VC を作成するスラッシュコマンド。

        処理の流れ:
          1. サーバー内でのみ実行可能かチェック
          2. defer() でインタラクションを即座に確認
          3. 「参加して作成」という名前の VC を新規作成
          4. DB にロビーとして登録
          5. 管理者に完了メッセージを表示
        """
        # DM (ダイレクトメッセージ) からの実行を拒否
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        # インタラクションを即座に確認 (複数インスタンス実行時の重複防止)
        # Discord は1つのインタラクションに対して1回しか応答を許可しないため、
        # 先に defer() した方だけが処理を続行できる
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.HTTPException, discord.InteractionResponded):
            return

        guild_id = str(interaction.guild_id)

        # ギルド単位のロックで重複作成を防止
        async with get_resource_lock(f"lobby_create:{guild_id}"):
            # --- 重複チェック ---
            # 1サーバーにつきロビーは1つまで
            async with async_session() as session:
                existing = await get_lobbies_by_guild(session, guild_id)
                if existing:
                    # Discord 上にチャンネルが実在するか確認
                    lobby = existing[0]
                    channel = interaction.guild.get_channel(int(lobby.lobby_channel_id))
                    if channel is not None:
                        await interaction.followup.send(
                            "このサーバーには既にロビーが存在します。",
                            ephemeral=True,
                        )
                        return
                    # チャンネルが削除済み → 孤立レコードを掃除
                    await delete_lobby(session, lobby.id)
                    if self._lobby_channel_ids is not None:
                        self._lobby_channel_ids.discard(lobby.lobby_channel_id)

            # --- VC の作成 ---
            try:
                lobby_channel = await interaction.guild.create_voice_channel(
                    name="➕ 新規VC作成",
                    rtc_region=DEFAULT_RTC_REGION,
                )
            except discord.HTTPException as e:
                await interaction.followup.send(
                    f"VCの作成に失敗しました: {e}", ephemeral=True
                )
                return

            # --- DB にロビーとして登録 ---
            lobby_channel_id_str = str(lobby_channel.id)
            async with async_session() as session:
                await create_lobby(
                    session,
                    guild_id=guild_id,
                    lobby_channel_id=lobby_channel_id_str,
                    category_id=None,
                    default_user_limit=0,
                )

            # キャッシュに追加
            if self._lobby_channel_ids is not None:
                self._lobby_channel_ids.add(lobby_channel_id_str)

        await interaction.followup.send(
            f"ロビー **{lobby_channel.name}** を作成しました！\n"
            f"お好みのカテゴリに手動で移動してください。",
            ephemeral=True,
        )

    @vc_group.command(name="panel", description="コントロールパネルを再投稿します")
    @app_commands.checks.cooldown(1, 30)
    async def vc_panel(self, interaction: discord.Interaction) -> None:
        """コントロールパネルの Embed + ボタンを再投稿するスラッシュコマンド。

        旧パネルメッセージを削除し、新しいパネルを送信する。
        一時 VC 内であれば誰でも実行可能。
        """
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "一時 VC 内で使用してください。", ephemeral=True
            )
            return

        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel.id))
            if not voice_session:
                await interaction.response.send_message(
                    "一時 VC が見つかりません。", ephemeral=True
                )
                return

        await repost_panel(channel, self.bot)
        await interaction.response.send_message(
            "コントロールパネルを再投稿しました。", ephemeral=True
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """スラッシュコマンドのエラーハンドラ。クールダウン中の通知を行う。"""
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"クールダウン中です。{error.retry_after:.0f}秒後に再実行できます。",
                ephemeral=True,
            )
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    """Cog を Bot に登録する関数。bot.load_extension() から呼ばれる。"""
    cog = VoiceCog(bot)
    await bot.add_cog(cog)

    # ロビーチャンネル ID のキャッシュを構築
    try:
        async with async_session() as session:
            lobbies = await get_all_lobbies(session)
            cog._lobby_channel_ids = {lobby.lobby_channel_id for lobby in lobbies}
        logger.info(
            "Loaded %d lobby channel(s) into cache",
            len(cog._lobby_channel_ids),
        )
    except Exception:
        logger.critical("Failed to load lobby cache", exc_info=True)
