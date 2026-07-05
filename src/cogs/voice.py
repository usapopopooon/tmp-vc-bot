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
import unicodedata
import weakref
from dataclasses import dataclass
from typing import Any, Literal, TypeGuard
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.lobby_config import (
    FEATURE_PRESET_FULL,
    FEATURE_PRESET_LIMIT_ONLY,
    LOBBY_CONTROL_ADMINS,
    LOBBY_CONTROL_MEMBERS,
    LOBBY_CONTROL_OWNER,
    LOBBY_NAMING_NUMBERED,
    LOBBY_NAMING_PERSONAL,
    LOBBY_OWNER_MODE_NONE,
    LOBBY_OWNER_MODE_OWNER,
    NUMBER_MATCH_BOTH,
    NUMBER_MATCH_FULL,
    NUMBER_MATCH_HALF,
    NUMBER_STYLE_FULL,
    NUMBER_STYLE_HALF,
    feature_flags_for_preset,
    format_sequence_number,
    has_owner,
    is_numbered_lobby,
    parse_sequence_number,
)
from src.database.engine import async_session
from src.database.models import Lobby, VoiceSession
from src.services.db_service import (
    add_voice_notify_exclude,
    add_voice_session_member,
    claim_event,
    create_lobby,
    create_voice_session,
    delete_lobbies_by_guild,
    delete_lobby,
    delete_voice_notify_by_channel,
    delete_voice_notify_by_guild,
    delete_voice_notify_category_config,
    delete_voice_notify_config,
    delete_voice_notify_exclude,
    delete_voice_session,
    delete_voice_sessions_by_guild,
    get_all_lobbies,
    get_lobbies_by_guild,
    get_lobby_by_channel_id,
    get_voice_notify_category_config,
    get_voice_notify_cross_guild_config,
    get_voice_session,
    get_voice_session_members_ordered,
    get_voice_sessions_by_lobby,
    is_voice_notify_excluded,
    list_voice_notify_category_configs,
    list_voice_notify_configs,
    list_voice_notify_configs_by_voice_channel,
    list_voice_notify_cross_guild_receivers,
    list_voice_notify_excludes,
    remove_voice_session_member,
    set_voice_notify_category_config,
    set_voice_notify_config,
    set_voice_notify_cross_guild_channel,
    set_voice_notify_cross_guild_invite_url,
    set_voice_notify_cross_guild_share,
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

_cross_guild_voice_notify_bots: weakref.WeakSet[commands.Bot] = weakref.WeakSet()
_CROSS_GUILD_VOICE_NOTIFY_READY_WAIT_SECONDS = 5.0
_DISCORD_API_BASE_URL = "https://discord.com/api/v10"


def register_cross_guild_voice_notify_bot(bot: commands.Bot) -> None:
    """同一プロセス内の Bot をクロス通知の送信候補として登録する。"""
    _cross_guild_voice_notify_bots.add(bot)


def unregister_cross_guild_voice_notify_bot(bot: commands.Bot) -> None:
    """クロス通知の送信候補から Bot を外す。"""
    _cross_guild_voice_notify_bots.discard(bot)


_LEGACY_LOBBY_NAME = "➕ 新規VC作成"
_DIALOG_DEFAULT_LOBBY_NAME = "作業空間作成"
_DIALOG_DEFAULT_ROOM_PREFIX = "作業空間"

_FEATURE_OVERRIDE_FIELDS = (
    "allow_rename",
    "allow_limit",
    "allow_bitrate",
    "allow_region",
    "allow_lock",
    "allow_hide",
    "allow_nsfw",
    "allow_transfer",
    "allow_kick",
    "allow_dissolve",
    "allow_block",
    "allow_allow",
    "allow_camera",
)

VOICE_NOTIFY_STATUS_LIST_LIMIT = 20

VoiceNotifyEventType = Literal["join", "leave"]


@dataclass(frozen=True)
class LobbyCreateConfig:
    """ロビー作成に使う設定値。slash command と Modal で共有する。"""

    lobby_name: str
    naming_mode: str
    room_prefix: str | None
    number_style: str
    number_match_mode: str
    start_number: int
    owner_mode: str
    control_policy: str
    feature_preset: str
    default_user_limit: int
    feature_overrides: dict[str, bool | None]


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
    if not isinstance(overwrite, discord.PermissionOverwrite):
        return discord.PermissionOverwrite()
    allow, deny = overwrite.pair()
    return discord.PermissionOverwrite.from_pair(allow, deny)


async def _update_permission_overwrite(
    channel: discord.VoiceChannel,
    target: discord.Member | discord.Role,
    **permissions: bool | None,
) -> None:
    """既存の PermissionOverwrite を保ったまま一部の権限だけ更新する。"""
    overwrite = _copy_overwrite(channel.overwrites_for(target))
    overwrite.update(**permissions)
    if overwrite.is_empty():
        await channel.set_permissions(target, overwrite=None)
    else:
        await channel.set_permissions(target, overwrite=overwrite)


def _sequence_scan_channels(
    guild: discord.Guild,
    category: discord.CategoryChannel | None,
) -> list[discord.VoiceChannel]:
    """連番の既存使用状況を調べる対象チャンネルを返す。"""
    if category is not None:
        return list(category.voice_channels)
    return list(guild.voice_channels)


def _used_sequence_numbers(
    lobby: Lobby,
    channels: list[discord.VoiceChannel],
    voice_sessions: list[VoiceSession],
) -> set[int]:
    """DB と Discord 上の実チャンネル名から使用済み連番を集める。"""
    used = {
        session.sequence_number
        for session in voice_sessions
        if session.sequence_number is not None
    }
    prefix = lobby.room_prefix or ""
    for existing_channel in channels:
        parsed = parse_sequence_number(
            existing_channel.name,
            prefix,
            lobby.number_match_mode,
        )
        if parsed is not None:
            used.add(parsed)
    return used


def _next_sequence_number(
    lobby: Lobby,
    channels: list[discord.VoiceChannel],
    voice_sessions: list[VoiceSession],
) -> int:
    """空いている最小の連番を返す。"""
    used = _used_sequence_numbers(lobby, channels, voice_sessions)
    candidate = max(lobby.start_number, 1)
    while candidate in used:
        candidate += 1
    return candidate


def _voice_channel_name(
    lobby: Lobby,
    member: discord.Member,
    sequence_number: int | None,
) -> str:
    """ロビー設定から作成する VC 名を決定する。"""
    if is_numbered_lobby(lobby):
        if sequence_number is None:
            msg = "numbered lobby requires sequence_number"
            raise ValueError(msg)
        prefix = lobby.room_prefix or "作業空間"
        suffix = format_sequence_number(sequence_number, lobby.number_style)
        return f"{prefix}{suffix}"
    return f"{member.display_name}'s channel"


def _is_legacy_lobby_request(
    *,
    lobby_name: str,
    naming_mode: str,
    room_prefix: str | None,
    number_style: str,
    number_match_mode: str,
    start_number: int,
    owner_mode: str,
    control_policy: str,
    feature_preset: str,
    default_user_limit: int,
    feature_overrides: dict[str, bool | None],
) -> bool:
    """引数なし相当の従来ロビー作成かどうかを判定する。"""
    return (
        lobby_name == _LEGACY_LOBBY_NAME
        and naming_mode == LOBBY_NAMING_PERSONAL
        and room_prefix is None
        and number_style == NUMBER_STYLE_HALF
        and number_match_mode == NUMBER_MATCH_BOTH
        and start_number == 1
        and owner_mode == LOBBY_OWNER_MODE_OWNER
        and control_policy == LOBBY_CONTROL_OWNER
        and feature_preset == FEATURE_PRESET_FULL
        and default_user_limit == 0
        and all(value is None for value in feature_overrides.values())
    )


def _resolve_feature_flags(
    feature_preset: str,
    overrides: dict[str, bool | None],
) -> dict[str, bool]:
    """プリセットと個別指定からロビー機能フラグを決定する。"""
    flags = feature_flags_for_preset(feature_preset)
    for field, value in overrides.items():
        if value is not None:
            flags[field] = value
    return flags


def _empty_feature_overrides() -> dict[str, bool | None]:
    """機能の個別上書きなしを表す辞書を返す。"""
    overrides: dict[str, bool | None] = {}
    for field in _FEATURE_OVERRIDE_FIELDS:
        overrides[field] = None
    return overrides


def _normalize_modal_text(value: object) -> str:
    """Modal 入力を NFKC 正規化し、前後空白を除去する。"""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _parse_modal_start_number(value: object) -> int:
    """Modal の開始番号を 1 以上の整数として読む。"""
    text = _normalize_modal_text(value)
    try:
        number = int(text)
    except ValueError:
        msg = "開始番号は数字で入力してください。"
        raise ValueError(msg) from None
    if number < 1:
        msg = "開始番号は 1 以上で入力してください。"
        raise ValueError(msg)
    return number


def _parse_modal_number_style(value: object) -> str:
    """Modal の数字形式入力を内部値へ変換する。"""
    text = _normalize_modal_text(value).lower()
    if text in {"半角", "hankaku", "half"}:
        return NUMBER_STYLE_HALF
    if text in {"全角", "zenkaku", "full"}:
        return NUMBER_STYLE_FULL
    msg = "数字形式は「半角」または「全角」で入力してください。"
    raise ValueError(msg)


def _parse_modal_feature_preset(value: object) -> str:
    """Modal の機能プリセット入力を内部値へ変換する。"""
    text = _normalize_modal_text(value).lower()
    if text in {"人数のみ", "人数", "limit", "limit_only"}:
        return FEATURE_PRESET_LIMIT_ONLY
    if text in {"全機能", "全部", "full"}:
        return FEATURE_PRESET_FULL
    msg = "機能は「人数のみ」または「全機能」で入力してください。"
    raise ValueError(msg)


def _dialog_lobby_name(lobby_name: str) -> str:
    """Dialog のロビー名デフォルトを返す。"""
    if lobby_name == _LEGACY_LOBBY_NAME:
        return _DIALOG_DEFAULT_LOBBY_NAME
    return lobby_name


def _dialog_room_prefix(lobby_name: str, room_prefix: str | None) -> str:
    """Dialog の部屋名プレフィックスデフォルトを返す。"""
    if room_prefix:
        return room_prefix
    if lobby_name != _LEGACY_LOBBY_NAME and lobby_name.endswith("作成"):
        prefix = lobby_name.removesuffix("作成")
        if prefix:
            return prefix
    return _DIALOG_DEFAULT_ROOM_PREFIX


def create_voice_notify_message(
    member: discord.Member,
    voice_channel_id: int | str,
    event_type: VoiceNotifyEventType,
) -> str:
    """VC 入退室通知の本文を作成する。"""
    display_name = discord.utils.escape_markdown(member.display_name)
    if event_type == "join":
        return f"{display_name} さんが <#{voice_channel_id}> に入室しました。"
    return f"{display_name} さんが <#{voice_channel_id}> から退室しました。"


def _escape_voice_notify_text(value: str) -> str:
    """通知本文に埋め込むプレーンテキストをエスケープする。"""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def _voice_notify_invite_link(label: str, invite_url: str | None) -> str:
    """招待 URL があればラベルを Discord のマスクリンクにする。"""
    escaped_label = (
        _escape_voice_notify_text(label).replace("[", r"\[").replace("]", r"\]")
    )
    if invite_url is None:
        return escaped_label
    return f"[{escaped_label}]({invite_url})"


def _is_discord_invite_url(value: str) -> bool:
    """Discord の招待 URL として扱ってよいかを判定する。"""
    parsed = urlparse(value)
    if parsed.scheme != "https":
        return False

    host = parsed.netloc.lower()
    if host == "discord.gg":
        return bool(parsed.path.strip("/"))
    if host in {"discord.com", "www.discord.com", "discordapp.com"}:
        path_parts = [part for part in parsed.path.split("/") if part]
        return len(path_parts) >= 2 and path_parts[0] == "invite"
    return False


def create_cross_guild_voice_notify_message(
    guild: discord.Guild,
    member: discord.Member,
    voice_channel: discord.VoiceChannel | discord.StageChannel,
    event_type: VoiceNotifyEventType,
    invite_url: str | None = None,
) -> str:
    """サーバー間 VC 入退室通知の本文を作成する。"""
    guild_name = _voice_notify_invite_link(guild.name, invite_url)
    channel_name = _escape_voice_notify_text(voice_channel.name)
    display_name = _escape_voice_notify_text(member.display_name)
    if event_type == "join":
        return f"{display_name} さんが {guild_name} の {channel_name} に入室しました。"
    return f"{display_name} さんが {guild_name} の {channel_name} から退室しました。"


def _is_voice_notify_voice_channel(
    channel: object,
) -> TypeGuard[discord.VoiceChannel | discord.StageChannel]:
    """VC 入退室通知の監視対象チャンネルかを判定する。"""
    return isinstance(channel, discord.VoiceChannel | discord.StageChannel)


def _is_voice_notify_sendable_channel(
    channel: object,
) -> TypeGuard[discord.TextChannel]:
    """VC 入退室通知の送信先にできるチャンネルかを判定する。"""
    return isinstance(channel, discord.TextChannel)


def _voice_notify_category_id(channel: object) -> str | None:
    """VC/Stage のカテゴリ ID を文字列で返す。"""
    category_id = getattr(channel, "category_id", None)
    if isinstance(category_id, int):
        return str(category_id)
    category = getattr(channel, "category", None)
    category_id = getattr(category, "id", None)
    if isinstance(category_id, int):
        return str(category_id)
    return None


def _format_limited_voice_notify_lines(lines: list[str]) -> list[str]:
    """status 表示の件数を制限する。"""
    limited = lines[:VOICE_NOTIFY_STATUS_LIST_LIMIT]
    omitted_count = len(lines) - len(limited)
    if omitted_count > 0:
        limited.append(f"ほか {omitted_count} 件")
    return limited


def _can_bot_send_voice_notify(
    channel: discord.TextChannel,
    interaction: discord.Interaction,
) -> bool:
    """指定チャンネルへ Bot が通知を送れるかを確認する。"""
    guild = interaction.guild
    if guild is None:
        return False

    bot_member = guild.me
    if bot_member is None and interaction.client.user is not None:
        bot_member = guild.get_member(interaction.client.user.id)
    if bot_member is None:
        return True

    permissions = channel.permissions_for(bot_member)
    return permissions.view_channel and permissions.send_messages


class VoiceCog(commands.Cog):
    """ボイスチャンネルの作成・削除・オーナー管理を行う Cog。

    Cog = discord.py の機能モジュール。関連するイベントハンドラや
    コマンドをまとめて管理できる。bot.load_extension() で読み込む。
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        register_cross_guild_voice_notify_bot(bot)
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

    async def cog_unload(self) -> None:
        """Cog アンロード時にクロス通知用 Bot レジストリから解除する。"""
        unregister_cross_guild_voice_notify_bot(self.bot)

    def _iter_cross_guild_voice_notify_bots(self) -> list[commands.Bot]:
        """クロス通知の送信先探索に使う Bot を現在の Bot 優先で返す。"""
        bots: list[commands.Bot] = []
        seen_bot_ids: set[int] = set()
        for bot in [self.bot, *list(_cross_guild_voice_notify_bots)]:
            bot_identity = id(bot)
            if bot_identity in seen_bot_ids:
                continue
            seen_bot_ids.add(bot_identity)

            try:
                is_closed = bot.is_closed()
            except Exception:
                is_closed = False
            if is_closed is True:
                continue

            bots.append(bot)
        return bots

    async def _wait_for_cross_guild_voice_notify_bots(
        self,
        bots: list[commands.Bot],
    ) -> None:
        """クロス通知探索前に、同時起動中 Bot の ready を短時間だけ待つ。"""
        for bot in bots:
            try:
                if bot.is_ready():
                    continue
            except Exception:
                continue

            try:
                await asyncio.wait_for(
                    bot.wait_until_ready(),
                    timeout=_CROSS_GUILD_VOICE_NOTIFY_READY_WAIT_SECONDS,
                )
            except (TimeoutError, RuntimeError):
                continue

    def _format_cross_guild_voice_notify_lookup(
        self,
        guild_id: str,
        channel_id: str,
    ) -> str:
        """クロス通知の送信先探索に失敗したときの診断情報を作る。"""
        try:
            guild_id_int = int(guild_id)
            channel_id_int = int(channel_id)
        except ValueError:
            return "invalid_ids=true"

        details: list[str] = []
        for bot in self._iter_cross_guild_voice_notify_bots():
            bot_user = getattr(bot, "user", None)
            bot_user_id = getattr(bot_user, "id", "unknown")
            ready: bool | str
            try:
                ready = bot.is_ready()
            except Exception:
                ready = "unknown"

            try:
                guild = bot.get_guild(guild_id_int)
            except Exception:
                guild = None

            if guild is None:
                guilds = getattr(bot, "guilds", [])
                try:
                    guild_count: int | str = len(guilds)
                except TypeError:
                    guild_count = "unknown"
                details.append(
                    f"bot={bot_user_id} ready={ready} "
                    f"receiver_guild=no guild_count={guild_count}"
                )
                continue

            try:
                channel = guild.get_channel(channel_id_int)
            except Exception:
                channel = None
            channel_type = type(channel).__name__ if channel is not None else "none"
            permission_status = "unknown"
            if _is_voice_notify_sendable_channel(channel):
                bot_member = guild.me
                if bot_member is None and isinstance(bot_user_id, int):
                    bot_member = guild.get_member(bot_user_id)
                if bot_member is not None:
                    permissions = channel.permissions_for(bot_member)
                    permission_status = (
                        f"view={permissions.view_channel} "
                        f"send={permissions.send_messages}"
                    )

            details.append(
                f"bot={bot_user_id} ready={ready} "
                f"receiver_guild=yes channel={channel_type} "
                f"perms={permission_status}"
            )

        if not details:
            return "bots=none"
        return " | ".join(details)

    # ==========================================================================
    # イベントリスナー
    # ==========================================================================

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """クロス通知の切り分け用に Bot ごとの所属 guild を記録する。"""
        bot_user = getattr(self.bot, "user", None)
        bot_user_id = getattr(bot_user, "id", "unknown")
        guild_ids = sorted(guild.id for guild in self.bot.guilds)
        logger.info(
            "VoiceCog ready: bot=%s guilds=%s cross_registry=%d",
            bot_user_id,
            guild_ids,
            len(_cross_guild_voice_notify_bots),
        )

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
        if before.channel != after.channel:
            try:
                await self._handle_voice_notify_state_update(member, before, after)
            except Exception:
                logger.exception(
                    "Failed to handle voice notification: member=%s",
                    member.id,
                )

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
        channel_id_str = str(channel.id)
        async with async_session() as session:
            await delete_voice_notify_by_channel(
                session,
                str(channel.guild.id),
                channel_id_str,
            )

        if not isinstance(channel, discord.VoiceChannel):
            return

        # メモリキャッシュの参加記録を削除
        self._cleanup_channel_cache(channel.id)
        # DB のレコードをクリーンアップ (存在しなくても安全)
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
            notify_count = await delete_voice_notify_by_guild(session, guild_id)
            # 先にボイスセッションを削除 (外部キー制約のため)
            vs_count = await delete_voice_sessions_by_guild(session, guild_id)
            # 次にロビーを削除
            lobby_count = await delete_lobbies_by_guild(session, guild_id)

        if notify_count > 0 or vs_count > 0 or lobby_count > 0:
            logger.info(
                "Cleaned up %d voice notify setting(s), %d voice session(s), "
                "and %d lobby/lobbies "
                "for removed guild: guild=%s",
                notify_count,
                vs_count,
                lobby_count,
                guild_id,
            )

    # ==========================================================================
    # VC 入退室通知
    # ==========================================================================

    async def _handle_voice_notify_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """VC 入退室通知の対象イベントを処理する。"""
        if member.bot or before.channel == after.channel:
            return

        before_channel = (
            before.channel
            if before.channel and _is_voice_notify_voice_channel(before.channel)
            else None
        )
        after_channel = (
            after.channel
            if after.channel and _is_voice_notify_voice_channel(after.channel)
            else None
        )
        if before_channel is None and after_channel is None:
            return

        guild = member.guild
        if before_channel is not None:
            await self._send_voice_notification(
                guild,
                member,
                before_channel,
                "leave",
            )
            try:
                await self._send_cross_guild_voice_notification(
                    guild,
                    member,
                    before_channel,
                    "leave",
                )
            except Exception:
                logger.exception(
                    "Failed to handle cross-guild voice notification: member=%s",
                    member.id,
                )

        if after_channel is not None:
            await self._send_voice_notification(
                guild,
                member,
                after_channel,
                "join",
            )
            try:
                await self._send_cross_guild_voice_notification(
                    guild,
                    member,
                    after_channel,
                    "join",
                )
            except Exception:
                logger.exception(
                    "Failed to handle cross-guild voice notification: member=%s",
                    member.id,
                )

    async def _send_voice_notification(
        self,
        guild: discord.Guild,
        member: discord.Member,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        event_type: VoiceNotifyEventType,
    ) -> bool:
        """設定に従って VC 入退室通知を送信する。"""
        guild_id = str(guild.id)
        voice_channel_id = str(voice_channel.id)

        async with async_session() as session:
            voice_configs = await list_voice_notify_configs_by_voice_channel(
                session,
                guild_id,
                voice_channel_id,
            )

            category_config = None
            category_id = _voice_notify_category_id(voice_channel)
            if category_id is not None and not await is_voice_notify_excluded(
                session,
                guild_id,
                voice_channel_id,
            ):
                category_config = await get_voice_notify_category_config(
                    session,
                    guild_id,
                    category_id,
                )

        notify_channel_ids: list[str] = []
        seen_notify_channel_ids: set[str] = set()
        for config in voice_configs:
            if config.notify_channel_id in seen_notify_channel_ids:
                continue
            seen_notify_channel_ids.add(config.notify_channel_id)
            notify_channel_ids.append(config.notify_channel_id)
        if (
            category_config is not None
            and category_config.notify_channel_id not in seen_notify_channel_ids
        ):
            notify_channel_ids.append(category_config.notify_channel_id)

        if not notify_channel_ids:
            return False

        content = create_voice_notify_message(member, voice_channel_id, event_type)
        sent = False
        for notify_channel_id in notify_channel_ids:
            channel = await self._fetch_voice_notify_sendable_channel(
                guild,
                notify_channel_id,
            )
            if channel is None:
                logger.warning(
                    "Voice notify channel is not sendable: guild=%s voice=%s notify=%s",
                    guild_id,
                    voice_channel_id,
                    notify_channel_id,
                )
                continue

            try:
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent = True
            except discord.HTTPException as e:
                logger.warning(
                    "Failed to send voice notification: guild=%s voice=%s "
                    "notify=%s error=%s",
                    guild_id,
                    voice_channel_id,
                    notify_channel_id,
                    e,
                )

        return sent

    async def _fetch_voice_notify_sendable_channel(
        self,
        guild: discord.Guild,
        channel_id: str,
    ) -> discord.TextChannel | None:
        """通知送信可能なテキストチャンネルを取得する。"""
        try:
            channel_id_int = int(channel_id)
        except ValueError:
            return None

        channel = guild.get_channel(channel_id_int)
        if _is_voice_notify_sendable_channel(channel):
            return channel

        try:
            fetched = await guild.fetch_channel(channel_id_int)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            return None

        if _is_voice_notify_sendable_channel(fetched):
            return fetched
        return None

    async def _send_cross_guild_voice_notification(
        self,
        guild: discord.Guild,
        member: discord.Member,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        event_type: VoiceNotifyEventType,
    ) -> bool:
        """共有 ON のサーバーの VC 入退室を、受信設定済みサーバーへ通知する。"""
        guild_id = str(guild.id)

        async with async_session() as session:
            source_config = await get_voice_notify_cross_guild_config(
                session,
                guild_id,
            )
            if source_config is None or not source_config.share_enabled:
                return False

            receiver_configs = await list_voice_notify_cross_guild_receivers(
                session,
                exclude_guild_id=guild_id,
            )

        if not receiver_configs:
            return False

        invite_url = (
            source_config.invite_url
            if isinstance(source_config.invite_url, str) and source_config.invite_url
            else None
        )
        content = create_cross_guild_voice_notify_message(
            guild,
            member,
            voice_channel,
            event_type,
            invite_url=invite_url,
        )
        sent = False
        for config in receiver_configs:
            if config.notify_channel_id is None:
                continue
            channel = await self._fetch_cross_guild_voice_notify_channel(
                config.guild_id,
                config.notify_channel_id,
            )
            if channel is None:
                if await self._send_cross_guild_voice_notification_via_rest(
                    config.guild_id,
                    config.notify_channel_id,
                    content,
                ):
                    sent = True
                    continue

                logger.warning(
                    "Cross-guild voice notify channel is not sendable: "
                    "source_guild=%s receiver_guild=%s notify=%s lookup=%s",
                    guild_id,
                    config.guild_id,
                    config.notify_channel_id,
                    self._format_cross_guild_voice_notify_lookup(
                        config.guild_id,
                        config.notify_channel_id,
                    ),
                )
                continue

            try:
                await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent = True
            except discord.HTTPException as e:
                if await self._send_cross_guild_voice_notification_via_rest(
                    config.guild_id,
                    config.notify_channel_id,
                    content,
                ):
                    sent = True
                    continue

                logger.warning(
                    "Failed to send cross-guild voice notification: "
                    "source_guild=%s receiver_guild=%s notify=%s error=%s",
                    guild_id,
                    config.guild_id,
                    config.notify_channel_id,
                    e,
                )

        return sent

    async def _send_cross_guild_voice_notification_via_rest(
        self,
        guild_id: str,
        channel_id: str,
        content: str,
    ) -> bool:
        """Bot クライアントのキャッシュ経由で送れない場合に REST で送信する。"""
        from src.config import settings

        try:
            guild_id_int = int(guild_id)
        except ValueError:
            return False

        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as http:
            for token_index, token in enumerate(settings.discord_tokens, start=1):
                headers = {
                    "Authorization": f"Bot {token}",
                    "User-Agent": "tmp-vc-bot",
                }
                channel_url = f"{_DISCORD_API_BASE_URL}/channels/{channel_id}"
                try:
                    async with http.get(channel_url, headers=headers) as response:
                        if response.status in {403, 404}:
                            continue
                        if response.status >= 400:
                            body = await response.text()
                            logger.warning(
                                "Cross-guild REST channel lookup failed: "
                                "receiver_guild=%s notify=%s token_index=%s "
                                "status=%s body=%s",
                                guild_id,
                                channel_id,
                                token_index,
                                response.status,
                                body[:200],
                            )
                            continue
                        channel_payload = await response.json()
                except aiohttp.ClientError as e:
                    logger.warning(
                        "Cross-guild REST channel lookup error: "
                        "receiver_guild=%s notify=%s token_index=%s error=%s",
                        guild_id,
                        channel_id,
                        token_index,
                        e,
                    )
                    continue

                if int(channel_payload.get("guild_id", 0)) != guild_id_int:
                    continue

                message_url = f"{channel_url}/messages"
                try:
                    async with http.post(
                        message_url,
                        headers=headers,
                        json={
                            "content": content,
                            "allowed_mentions": {"parse": []},
                        },
                    ) as response:
                        if 200 <= response.status < 300:
                            return True
                        if response.status in {403, 404}:
                            continue
                        body = await response.text()
                        logger.warning(
                            "Cross-guild REST send failed: "
                            "receiver_guild=%s notify=%s token_index=%s "
                            "status=%s body=%s",
                            guild_id,
                            channel_id,
                            token_index,
                            response.status,
                            body[:200],
                        )
                except aiohttp.ClientError as e:
                    logger.warning(
                        "Cross-guild REST send error: "
                        "receiver_guild=%s notify=%s token_index=%s error=%s",
                        guild_id,
                        channel_id,
                        token_index,
                        e,
                    )

        return False

    async def _fetch_cross_guild_voice_notify_channel(
        self,
        guild_id: str,
        channel_id: str,
    ) -> discord.TextChannel | None:
        """サーバー間通知の送信先テキストチャンネルを取得する。"""
        try:
            guild_id_int = int(guild_id)
            channel_id_int = int(channel_id)
        except ValueError:
            return None

        bots = self._iter_cross_guild_voice_notify_bots()
        await self._wait_for_cross_guild_voice_notify_bots(bots)
        bots = self._iter_cross_guild_voice_notify_bots()

        for bot in bots:
            guild = bot.get_guild(guild_id_int)
            if guild is None:
                continue
            guild_channel = await self._fetch_voice_notify_sendable_channel(
                guild,
                channel_id,
            )
            if guild_channel is not None:
                return guild_channel

        for bot in bots:
            channel = bot.get_channel(channel_id_int)
            if (
                _is_voice_notify_sendable_channel(channel)
                and channel.guild.id == guild_id_int
            ):
                return channel

        for bot in bots:
            try:
                fetched = await bot.fetch_channel(channel_id_int)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                continue

            if (
                _is_voice_notify_sendable_channel(fetched)
                and fetched.guild.id == guild_id_int
            ):
                return fetched
        return None

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

    def _evaluate_member_restriction(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel,
        voice_session: VoiceSession,
        *,
        current_count: int | None = None,
    ) -> str | None:
        """メンバーが一時 VC の制限に違反しているかを判定する。

        判定ルール:
          - Bot / Administrator / オーナー本人は常に許可
          - 個別の connect=False overwrite (ブロック) があればキック
          - ロック中: 個別の connect=True がなければキック
          - 人数制限超過: Bot も人数枠として数え、overwrite に関係なく超過分はキック

        Args:
            member: 判定対象のメンバー
            channel: 対象の VC
            voice_session: 対応する VoiceSession
            current_count: 現在の人数 (省略時は channel.members から計算)

        Returns:
            違反している場合はキック理由の文字列、許可される場合は None。
        """
        if member.bot:
            return None
        if member.guild_permissions.administrator:
            return None
        if (
            voice_session.owner_id is not None
            and str(member.id) == voice_session.owner_id
        ):
            return None

        overwrites = channel.overwrites_for(member)

        # --- ブロック (個別 connect=False) チェック ---
        # 「メンバーを移動」権限を持つモデレーターが
        # ブロックされたユーザーを VC に放り込んだ場合に弾く。
        if overwrites.connect is False:
            return "ブロックされているため"

        # --- ロックチェック ---
        if voice_session.is_locked and overwrites.connect is not True:
            return "ロックされているため"

        # --- 人数制限チェック ---
        # 「メンバーを移動」権限による Discord の user_limit バイパスを
        # Bot 側で強制する。overwrite に関わらず超過分はキックする。
        user_limit = self._get_channel_user_limit(channel)
        if user_limit > 0:
            count = current_count if current_count is not None else len(channel.members)
            if count > user_limit:
                return "人数制限を超えているため"

        return None

    def _get_channel_user_limit(self, channel: discord.VoiceChannel) -> int:
        """人数制限は Discord のチャンネル設定だけを正とする。"""
        channel_limit = getattr(channel, "user_limit", None)
        if isinstance(channel_limit, int):
            return channel_limit
        return 0

    async def _kick_with_notification(
        self, member: discord.Member, channel: discord.VoiceChannel, reason: str
    ) -> bool:
        """メンバーをキックして本人とチャンネルに通知する。

        Returns:
            キックの move_to に成功したら True、失敗 (例: 権限不足) は False。
            通知失敗は戻り値に影響しない。
        """
        logger.info(
            "Kicking member %s from channel %s: %s",
            member.id,
            channel.id,
            reason,
        )
        moved = True
        try:
            await member.move_to(None)
        except discord.HTTPException as e:
            logger.warning(
                "Failed to kick member %s from channel %s: %s",
                member.id,
                channel.id,
                e,
            )
            moved = False

        # 通知は move_to の成否に関わらず送信する (既存挙動を維持)。
        try:
            await channel.send(f"⚠️ {member.mention} は{reason}入室できません。")
        except discord.HTTPException as e:
            logger.debug(
                "Failed to send kick notification to channel %s: %s",
                channel.id,
                e,
            )
        with contextlib.suppress(discord.HTTPException, discord.Forbidden):
            await member.send(f"⚠️ **{channel.name}** は{reason}入室できませんでした。")
        return moved

    async def _enforce_channel_restrictions(
        self, member: discord.Member, channel: discord.VoiceChannel
    ) -> bool:
        """一時 VC のブロック/ロック/人数制限を入室時に強制する。

        「メンバーを移動」権限を持つユーザーは Discord の仕様上、
        connect=False のチャンネルや user_limit に達したチャンネルにも
        入室できてしまう。このメソッドでは Administrator 権限を持たない
        ユーザーが制限を回避して入室した場合にキックする。

        Args:
            member: 参加したメンバー
            channel: 参加先のボイスチャンネル

        Returns:
            True: キックした (呼び出し元で後続処理をスキップすべき)
            False: キックしなかった (正常な入室)
        """
        # DB アクセス前に早期 return (Bot / Administrator は常に許可)。
        if member.bot:
            return False
        if member.guild_permissions.administrator:
            return False

        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel.id))
            if not voice_session:
                # 一時 VC ではない (ロビーなど)
                return False

            reason = self._evaluate_member_restriction(member, channel, voice_session)
            if not reason:
                # 制限違反なし。隠しチャンネルなら閲覧権限を付与しておく
                # (Bug D: 退出後も非表示のままにならないようにする)。
                await self._grant_view_if_hidden(member, channel, voice_session)
                return False

            # 重複排除テーブルで重複防止 (マルチインスタンス)
            bucket = int(time.time()) // 5
            event_key = f"vc_kick:{channel.id}:{member.id}:{bucket}"
            if not await claim_event(session, event_key):
                logger.info(
                    "VC kick already claimed by another instance: %s",
                    event_key,
                )
                return False
            await session.commit()

        await self._kick_with_notification(member, channel, reason)
        return True

    async def _grant_view_if_hidden(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel,
        voice_session: VoiceSession,
    ) -> None:
        """非表示チャンネルに参加したメンバーに閲覧権限を付与する。

        非表示モード時、参加したメンバーは VC 中はチャンネルが見える状態だが、
        個別 overwrite が無いと退出後に再度見えなくなる。在室中・以降の
        UX 一貫性のため、入室時に view_channel=True を設定する。
        """
        if not voice_session.is_hidden:
            return
        overwrites = channel.overwrites_for(member)
        if overwrites.view_channel is True:
            return
        try:
            await _update_permission_overwrite(channel, member, view_channel=True)
        except discord.HTTPException as e:
            logger.warning(
                "Failed to grant view_channel to %s on hidden channel %s: %s",
                member.id,
                channel.id,
                e,
            )

    async def enforce_all_members(
        self,
        channel: discord.VoiceChannel,
        *,
        user_limit_override: int | None = None,
    ) -> int:
        """チャンネル内の既存メンバー全員に制限を適用する (遡及キック)。

        ロックの ON 切替時や人数制限の引き下げ時に呼び出す。
        - ブロック/ロック違反は無条件にキック
        - 人数制限超過は新しい参加者から順にキック (Bot/オーナー/Admin は保護)

        Args:
            channel: 対象の VC
            user_limit_override: 直前に適用した人数制限。指定時は
                Discord.py のローカルチャンネルキャッシュより優先する。

        Returns:
            キックしたメンバー数
        """
        async with async_session() as session:
            voice_session = await get_voice_session(session, str(channel.id))
            if not voice_session:
                return 0

        kicked = 0

        # --- パス 1: ブロック / ロック違反 ---
        # 人数制限はあとで別途処理するため、ここでは current_count=0 を渡して
        # user_limit 判定をスキップさせる (limit > 0 でも 0 > limit にならない)。
        for member in list(channel.members):
            reason = self._evaluate_member_restriction(
                member, channel, voice_session, current_count=0
            )
            if (
                reason
                and reason != "人数制限を超えているため"
                and await self._kick_with_notification(member, channel, reason)
            ):
                kicked += 1

        # --- パス 2: 人数制限の超過分を新参者からキック ---
        user_limit = (
            user_limit_override
            if user_limit_override is not None
            else self._get_channel_user_limit(channel)
        )
        if user_limit > 0:
            remaining = list(channel.members)
            excess = len(remaining) - user_limit
            if excess > 0:
                kickable = [
                    m
                    for m in remaining
                    if not m.bot
                    and str(m.id) != voice_session.owner_id
                    and not m.guild_permissions.administrator
                ]
                # 参加時刻が新しい順 (= 後から入った人) を先にキック。
                # 記録のないメンバーは最古扱い (0.0) とし、保護されやすくする。
                join_times = self._join_times.get(channel.id, {})
                kickable.sort(key=lambda m: join_times.get(m.id, 0.0), reverse=True)
                for member in kickable[:excess]:
                    if await self._kick_with_notification(
                        member, channel, "人数制限を超えているため"
                    ):
                        kicked += 1

        return kicked

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
            # チャンネル名はロビー設定から決定する。
            # ロビーチャンネルの権限設定をコピーして
            # @everyone の接続拒否などを引き継ぐ
            # 高速化のため set_permissions() の追加 API 呼び出しを避け、
            # オーナーありの場合だけテキストチャット権限をオーナー専用にする。
            owner_id = str(member.id) if has_owner(lobby) else None
            new_channel: discord.VoiceChannel | None = None
            voice_session: VoiceSession | None = None

            async with get_resource_lock(f"lobby_sequence:{lobby.id}"):
                for _attempt in range(5):
                    sequence_number = None
                    if is_numbered_lobby(lobby):
                        active_sessions = await get_voice_sessions_by_lobby(
                            session, lobby.id
                        )
                        sequence_number = _next_sequence_number(
                            lobby,
                            _sequence_scan_channels(guild, category),
                            active_sessions,
                        )

                    channel_name = _voice_channel_name(lobby, member, sequence_number)
                    overwrites = dict(channel.overwrites)

                    if owner_id is not None:
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
                        overwrites=overwrites,
                    )

                    # --- DB にセッション記録 ---
                    # VC 作成に成功したら、DB にセッション情報を保存する。
                    # 失敗した場合は作成した VC を削除してクリーンアップ。
                    voice_session = None
                    try:
                        voice_session = await create_voice_session(
                            session,
                            lobby_id=lobby.id,
                            channel_id=str(new_channel.id),
                            owner_id=owner_id,
                            name=channel_name,
                            sequence_number=sequence_number,
                        )
                        # 最初のメンバーとして DB に登録
                        await add_voice_session_member(
                            session, voice_session.id, str(member.id)
                        )
                    except IntegrityError:
                        await session.rollback()
                        if voice_session is not None:
                            try:
                                await delete_voice_session(session, str(new_channel.id))
                            except Exception:
                                logger.exception(
                                    "Failed to cleanup voice session %s after "
                                    "member registration conflict",
                                    new_channel.id,
                                )
                            with contextlib.suppress(discord.HTTPException):
                                await new_channel.delete()
                            raise

                        await new_channel.delete()
                        new_channel = None
                        if not is_numbered_lobby(lobby):
                            raise
                        logger.info(
                            "Sequence conflict for lobby %s, retrying VC creation",
                            lobby.id,
                        )
                        continue
                    except Exception:
                        await session.rollback()
                        if voice_session is not None:
                            try:
                                await delete_voice_session(session, str(new_channel.id))
                            except Exception:
                                logger.exception(
                                    "Failed to cleanup voice session %s after "
                                    "member registration failure",
                                    new_channel.id,
                                )
                        with contextlib.suppress(discord.HTTPException):
                            await new_channel.delete()
                        raise
                    # VC 作成成功後、クールダウンを記録
                    record_vc_create_cooldown(member.id)
                    break
                else:
                    logger.error("Failed to allocate sequence for lobby %s", lobby.id)
                    with contextlib.suppress(discord.HTTPException, discord.Forbidden):
                        await member.send(
                            "VC の作成に失敗しました。少し待ってください。"
                        )
                    return

            if new_channel is None or voice_session is None:
                return

            # --- チャンネル初期化 ---
            # DB セッション作成後の全操作をまとめてエラーハンドリングする。
            # move_to, send のいずれかが失敗した場合、
            # 不完全なチャンネルと DB レコードを両方クリーンアップする。
            try:
                # 一時 VC は起点ユーザーごとに作成する。
                # ロビー内の他メンバーまで移動すると、同時参加時に別ユーザーの
                # 作成処理と競合して空 VC や所有者不在 VC ができる。
                await member.move_to(new_channel)

                # コントロールパネル (Embed + ボタン) を送信
                embed = create_control_panel_embed(
                    voice_session,
                    member if owner_id is not None else None,
                    user_limit=self._get_channel_user_limit(new_channel),
                    lobby=lobby,
                )
                view = ControlPanelView(
                    voice_session.id,
                    voice_session.is_locked,
                    voice_session.is_hidden,
                    lobby=lobby,
                )
                if view.children:
                    if isinstance(lobby, Lobby):
                        self.bot.add_view(
                            ControlPanelView(
                                voice_session.id,
                                voice_session.is_locked,
                                voice_session.is_hidden,
                            )
                        )
                    else:
                        self.bot.add_view(view)
                if view.children:
                    panel_msg = await new_channel.send(embed=embed, view=view)
                else:
                    panel_msg = await new_channel.send(embed=embed)

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
            await _update_permission_overwrite(
                channel, old_owner, read_message_history=None
            )
            await _update_permission_overwrite(
                channel, new_owner, read_message_history=True
            )
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

    async def _create_lobby_from_config(
        self,
        interaction: discord.Interaction,
        config: LobbyCreateConfig,
    ) -> None:
        """ロビー作成設定から VC と DB レコードを作成する。"""
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

        lobby_name = config.lobby_name.strip()
        room_prefix = config.room_prefix.strip() if config.room_prefix else None
        owner_mode = config.owner_mode
        control_policy = config.control_policy

        if not lobby_name:
            await interaction.followup.send(
                "ロビー名を入力してください。",
                ephemeral=True,
            )
            return
        if len(lobby_name) > 100:
            await interaction.followup.send(
                "ロビー名は 100 文字以内で入力してください。",
                ephemeral=True,
            )
            return
        if config.default_user_limit < 0 or config.default_user_limit > 99:
            await interaction.followup.send(
                "デフォルト人数制限は 0〜99 の範囲で指定してください。",
                ephemeral=True,
            )
            return
        if config.start_number < 1:
            await interaction.followup.send(
                "開始番号は 1 以上で指定してください。",
                ephemeral=True,
            )
            return
        if config.naming_mode == LOBBY_NAMING_NUMBERED and not room_prefix:
            await interaction.followup.send(
                "連番ロビーでは room_prefix を指定してください。",
                ephemeral=True,
            )
            return
        if config.naming_mode == LOBBY_NAMING_NUMBERED and room_prefix:
            first_room_name = room_prefix + format_sequence_number(
                config.start_number, config.number_style
            )
            if len(first_room_name) > 100:
                await interaction.followup.send(
                    "作成される VC 名は 100 文字以内にしてください。",
                    ephemeral=True,
                )
                return

        if (
            owner_mode == LOBBY_OWNER_MODE_NONE
            and control_policy == LOBBY_CONTROL_OWNER
        ):
            control_policy = LOBBY_CONTROL_MEMBERS

        feature_flags = _resolve_feature_flags(
            config.feature_preset,
            config.feature_overrides,
        )
        if owner_mode == LOBBY_OWNER_MODE_NONE:
            feature_flags["allow_transfer"] = False

        is_legacy_request = _is_legacy_lobby_request(
            lobby_name=lobby_name,
            naming_mode=config.naming_mode,
            room_prefix=room_prefix,
            number_style=config.number_style,
            number_match_mode=config.number_match_mode,
            start_number=config.start_number,
            owner_mode=owner_mode,
            control_policy=control_policy,
            feature_preset=config.feature_preset,
            default_user_limit=config.default_user_limit,
            feature_overrides=config.feature_overrides,
        )

        guild_id = str(interaction.guild_id)
        # ギルド単位のロックで重複作成を防止
        async with get_resource_lock(f"lobby_create:{guild_id}"):
            # --- 重複チェック ---
            # 引数なしの従来ロビー作成だけは後方互換として 1 サーバー 1 件を維持。
            # 設定付きロビーは通常ロビーと併存できる。
            async with async_session() as session:
                if is_legacy_request:
                    existing = await get_lobbies_by_guild(session, guild_id)
                    for lobby in existing:
                        channel = interaction.guild.get_channel(
                            int(lobby.lobby_channel_id)
                        )
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
                    name=lobby_name,
                    rtc_region=DEFAULT_RTC_REGION,
                )
            except discord.HTTPException as e:
                await interaction.followup.send(
                    f"VCの作成に失敗しました: {e}", ephemeral=True
                )
                return

            # --- DB にロビーとして登録 ---
            lobby_channel_id_str = str(lobby_channel.id)
            try:
                async with async_session() as session:
                    if is_legacy_request:
                        await create_lobby(
                            session,
                            guild_id=guild_id,
                            lobby_channel_id=lobby_channel_id_str,
                            category_id=None,
                            default_user_limit=0,
                        )
                    else:
                        await create_lobby(
                            session,
                            guild_id=guild_id,
                            lobby_channel_id=lobby_channel_id_str,
                            category_id=None,
                            default_user_limit=config.default_user_limit,
                            naming_mode=config.naming_mode,
                            room_prefix=room_prefix,
                            number_style=config.number_style,
                            number_match_mode=config.number_match_mode,
                            start_number=config.start_number,
                            owner_mode=owner_mode,
                            control_policy=control_policy,
                            **feature_flags,
                        )
            except Exception:
                logger.exception(
                    "Failed to register lobby channel %s in database",
                    lobby_channel.id,
                )
                try:
                    await lobby_channel.delete()
                except discord.HTTPException as e:
                    logger.warning(
                        "Failed to delete lobby channel %s after DB error: %s",
                        lobby_channel.id,
                        e,
                    )
                    await interaction.followup.send(
                        "ロビーの登録に失敗しました。"
                        "作成した VC の削除にも失敗したため、手動で削除してください。",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        "ロビーの登録に失敗したため、作成した VC を削除しました。",
                        ephemeral=True,
                    )
                return

            # キャッシュに追加
            if self._lobby_channel_ids is not None:
                self._lobby_channel_ids.add(lobby_channel_id_str)

        await interaction.followup.send(
            f"ロビー **{lobby_channel.name}** を作成しました！\n"
            f"お好みのカテゴリに手動で移動してください。",
            ephemeral=True,
        )

    # ==========================================================================
    # スラッシュコマンド (/vc グループ)
    # ==========================================================================

    vc_group = app_commands.Group(
        name="vc",
        description="一時 VC の管理コマンド",
    )
    voice_notify_group = app_commands.Group(
        name="voice-notify",
        description="VC入退室通知を管理します",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    voice_notify_cross_group = app_commands.Group(
        name="voice-notify-cross",
        description="サーバー間VC入退室通知を管理します",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )

    @vc_group.command(name="lobby", description="ロビーVCを作成します")
    @app_commands.describe(dialog="連番共有ロビーをダイアログで設定します")
    @app_commands.choices(
        naming_mode=[
            app_commands.Choice(name="個人名", value=LOBBY_NAMING_PERSONAL),
            app_commands.Choice(name="連番", value=LOBBY_NAMING_NUMBERED),
        ],
        number_style=[
            app_commands.Choice(name="半角", value=NUMBER_STYLE_HALF),
            app_commands.Choice(name="全角", value=NUMBER_STYLE_FULL),
        ],
        number_match_mode=[
            app_commands.Choice(name="半角のみ", value=NUMBER_MATCH_HALF),
            app_commands.Choice(name="全角のみ", value=NUMBER_MATCH_FULL),
            app_commands.Choice(name="両方", value=NUMBER_MATCH_BOTH),
        ],
        owner_mode=[
            app_commands.Choice(name="オーナーあり", value=LOBBY_OWNER_MODE_OWNER),
            app_commands.Choice(name="オーナーなし", value=LOBBY_OWNER_MODE_NONE),
        ],
        control_policy=[
            app_commands.Choice(name="オーナーのみ", value=LOBBY_CONTROL_OWNER),
            app_commands.Choice(name="VC参加者", value=LOBBY_CONTROL_MEMBERS),
            app_commands.Choice(name="管理者のみ", value=LOBBY_CONTROL_ADMINS),
        ],
        feature_preset=[
            app_commands.Choice(name="全機能", value=FEATURE_PRESET_FULL),
            app_commands.Choice(name="人数のみ", value=FEATURE_PRESET_LIMIT_ONLY),
        ],
    )
    @app_commands.default_permissions(administrator=True)
    async def vc_lobby(
        self,
        interaction: discord.Interaction,
        dialog: bool = False,
        lobby_name: str = _LEGACY_LOBBY_NAME,
        naming_mode: str = LOBBY_NAMING_PERSONAL,
        room_prefix: str | None = None,
        number_style: str = NUMBER_STYLE_HALF,
        number_match_mode: str = NUMBER_MATCH_BOTH,
        start_number: int = 1,
        owner_mode: str = LOBBY_OWNER_MODE_OWNER,
        control_policy: str = LOBBY_CONTROL_OWNER,
        feature_preset: str = FEATURE_PRESET_FULL,
        default_user_limit: int = 0,
        allow_rename: bool | None = None,
        allow_limit: bool | None = None,
        allow_bitrate: bool | None = None,
        allow_region: bool | None = None,
        allow_lock: bool | None = None,
        allow_hide: bool | None = None,
        allow_nsfw: bool | None = None,
        allow_transfer: bool | None = None,
        allow_kick: bool | None = None,
        allow_dissolve: bool | None = None,
        allow_block: bool | None = None,
        allow_connect: bool | None = None,
        allow_camera: bool | None = None,
    ) -> None:
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

        if dialog:
            await interaction.response.send_modal(
                NumberedLobbyModal(
                    self,
                    lobby_name=_dialog_lobby_name(lobby_name),
                    room_prefix=_dialog_room_prefix(lobby_name, room_prefix),
                    start_number=start_number,
                    number_style=number_style,
                    feature_preset=FEATURE_PRESET_LIMIT_ONLY,
                )
            )
            return

        feature_overrides = {
            "allow_rename": allow_rename,
            "allow_limit": allow_limit,
            "allow_bitrate": allow_bitrate,
            "allow_region": allow_region,
            "allow_lock": allow_lock,
            "allow_hide": allow_hide,
            "allow_nsfw": allow_nsfw,
            "allow_transfer": allow_transfer,
            "allow_kick": allow_kick,
            "allow_dissolve": allow_dissolve,
            "allow_block": allow_block,
            "allow_allow": allow_connect,
            "allow_camera": allow_camera,
        }
        await self._create_lobby_from_config(
            interaction,
            LobbyCreateConfig(
                lobby_name=lobby_name,
                naming_mode=naming_mode,
                room_prefix=room_prefix,
                number_style=number_style,
                number_match_mode=number_match_mode,
                start_number=start_number,
                owner_mode=owner_mode,
                control_policy=control_policy,
                feature_preset=feature_preset,
                default_user_limit=default_user_limit,
                feature_overrides=feature_overrides,
            ),
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

    async def _ensure_voice_notify_guild(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        """voice-notify コマンドがサーバー内で実行されたか確認する。"""
        if interaction.guild is None or interaction.guild_id is None:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ実行できます。",
                ephemeral=True,
            )
            return False
        return True

    @voice_notify_group.command(
        name="add",
        description="VC入退室通知を追加または更新します",
    )
    async def voice_notify_add(
        self,
        interaction: discord.Interaction,
        voice: discord.VoiceChannel | discord.StageChannel,
        notify: discord.TextChannel,
    ) -> None:
        """VC 単位の入退室通知を追加または更新する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return
        if not _can_bot_send_voice_notify(notify, interaction):
            await interaction.response.send_message(
                "そのチャンネルに送信する権限が Bot にありません。",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        async with async_session() as session:
            await set_voice_notify_config(
                session,
                guild_id,
                str(voice.id),
                str(notify.id),
            )

        await interaction.response.send_message(
            "\n".join(
                [
                    "VC入退室通知を設定しました。",
                    f"監視VC: <#{voice.id}>",
                    f"通知先: <#{notify.id}>",
                ]
            ),
            ephemeral=True,
        )

    @voice_notify_group.command(
        name="remove",
        description="VC入退室通知を解除します",
    )
    async def voice_notify_remove(
        self,
        interaction: discord.Interaction,
        voice: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        """VC 単位の入退室通知を削除する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            removed = await delete_voice_notify_config(
                session,
                str(interaction.guild_id),
                str(voice.id),
            )

        content = (
            f"VC入退室通知を解除しました。監視VC: <#{voice.id}>"
            if removed
            else f"そのVCの入退室通知は設定されていません。監視VC: <#{voice.id}>"
        )
        await interaction.response.send_message(content, ephemeral=True)

    @voice_notify_group.command(
        name="add-category",
        description="カテゴリ内VCの入退室通知を追加または更新します",
    )
    async def voice_notify_add_category(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        notify: discord.TextChannel,
    ) -> None:
        """カテゴリ単位の入退室通知を追加または更新する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return
        if not _can_bot_send_voice_notify(notify, interaction):
            await interaction.response.send_message(
                "そのチャンネルに送信する権限が Bot にありません。",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        async with async_session() as session:
            await set_voice_notify_category_config(
                session,
                guild_id,
                str(category.id),
                str(notify.id),
            )

        await interaction.response.send_message(
            "\n".join(
                [
                    "VCカテゴリ入退室通知を設定しました。",
                    f"監視カテゴリ: <#{category.id}>",
                    f"通知先: <#{notify.id}>",
                ]
            ),
            ephemeral=True,
        )

    @voice_notify_group.command(
        name="remove-category",
        description="カテゴリ内VCの入退室通知を解除します",
    )
    async def voice_notify_remove_category(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
    ) -> None:
        """カテゴリ単位の入退室通知を削除する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            removed = await delete_voice_notify_category_config(
                session,
                str(interaction.guild_id),
                str(category.id),
            )

        content = (
            f"VCカテゴリ入退室通知を解除しました。監視カテゴリ: <#{category.id}>"
            if removed
            else "そのカテゴリの入退室通知は設定されていません。"
            f"監視カテゴリ: <#{category.id}>"
        )
        await interaction.response.send_message(content, ephemeral=True)

    @voice_notify_group.command(
        name="exclude-add",
        description="カテゴリ通知から除外するVCを追加します",
    )
    async def voice_notify_exclude_add(
        self,
        interaction: discord.Interaction,
        voice: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        """カテゴリ通知の除外 VC を追加する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        guild_id = str(interaction.guild_id)
        async with async_session() as session:
            await add_voice_notify_exclude(session, guild_id, str(voice.id))

        await interaction.response.send_message(
            f"カテゴリ通知の除外VCに追加しました。除外VC: <#{voice.id}>",
            ephemeral=True,
        )

    @voice_notify_group.command(
        name="exclude-remove",
        description="カテゴリ通知の除外VCを解除します",
    )
    async def voice_notify_exclude_remove(
        self,
        interaction: discord.Interaction,
        voice: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        """カテゴリ通知の除外 VC を削除する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            removed = await delete_voice_notify_exclude(
                session,
                str(interaction.guild_id),
                str(voice.id),
            )

        content = (
            f"カテゴリ通知の除外VCを解除しました。除外VC: <#{voice.id}>"
            if removed
            else "そのVCはカテゴリ通知の除外対象に設定されていません。"
            f"除外VC: <#{voice.id}>"
        )
        await interaction.response.send_message(content, ephemeral=True)

    @voice_notify_group.command(
        name="status",
        description="VC入退室通知の設定を表示します",
    )
    async def voice_notify_status(self, interaction: discord.Interaction) -> None:
        """VC 入退室通知設定を表示する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            configs = await list_voice_notify_configs(
                session,
                str(interaction.guild_id),
            )
            category_configs = await list_voice_notify_category_configs(
                session,
                str(interaction.guild_id),
            )
            excludes = await list_voice_notify_excludes(
                session,
                str(interaction.guild_id),
            )

        if not configs and not category_configs and not excludes:
            await interaction.response.send_message(
                "VC入退室通知は設定されていません。",
                ephemeral=True,
            )
            return

        lines = ["VC入退室通知の設定:"]
        fixed_lines = _format_limited_voice_notify_lines(
            [
                f"・<#{config.voice_channel_id}> -> <#{config.notify_channel_id}>"
                for config in configs
            ]
        )
        category_lines = _format_limited_voice_notify_lines(
            [
                f"・<#{config.category_id}> -> <#{config.notify_channel_id}>"
                for config in category_configs
            ]
        )
        exclude_lines = _format_limited_voice_notify_lines(
            [f"・<#{exclude.voice_channel_id}>" for exclude in excludes]
        )

        if fixed_lines:
            lines.extend(["固定VC:", *fixed_lines])
        if category_lines:
            lines.extend(["カテゴリ:", *category_lines])
        if exclude_lines:
            lines.extend(["カテゴリ通知の除外VC:", *exclude_lines])

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @voice_notify_cross_group.command(
        name="share",
        description="このサーバーのVC入退室を他サーバーへ共有するか設定します",
    )
    async def voice_notify_cross_share(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        """このサーバーの VC 入退室をサーバー間通知へ共有するか設定する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            await set_voice_notify_cross_guild_share(
                session,
                str(interaction.guild_id),
                enabled,
            )

        status = "ON" if enabled else "OFF"
        await interaction.response.send_message(
            f"サーバー間VC入退室共有を {status} にしました。",
            ephemeral=True,
        )

    @voice_notify_cross_group.command(
        name="receive",
        description="他サーバーから共有されたVC入退室通知の受信先を設定します",
    )
    async def voice_notify_cross_receive(
        self,
        interaction: discord.Interaction,
        notify: discord.TextChannel,
    ) -> None:
        """他サーバーから共有された VC 入退室通知の受信先を設定する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return
        if not _can_bot_send_voice_notify(notify, interaction):
            await interaction.response.send_message(
                "そのチャンネルに送信する権限が Bot にありません。",
                ephemeral=True,
            )
            return

        async with async_session() as session:
            await set_voice_notify_cross_guild_channel(
                session,
                str(interaction.guild_id),
                str(notify.id),
            )

        await interaction.response.send_message(
            f"サーバー間VC入退室通知の受信先を設定しました。通知先: <#{notify.id}>",
            ephemeral=True,
        )

    @voice_notify_cross_group.command(
        name="invite",
        description="サーバー間通知のサーバー名リンクに使う招待URLを設定します",
    )
    async def voice_notify_cross_invite(
        self,
        interaction: discord.Interaction,
        url: str,
    ) -> None:
        """サーバー間 VC 入退室通知で使う固定招待 URL を設定する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        invite_url = url.strip()
        if not _is_discord_invite_url(invite_url):
            await interaction.response.send_message(
                "Discord の招待URLを指定してください。",
                ephemeral=True,
            )
            return

        async with async_session() as session:
            await set_voice_notify_cross_guild_invite_url(
                session,
                str(interaction.guild_id),
                invite_url,
            )

        await interaction.response.send_message(
            "サーバー間VC入退室通知の招待URLを設定しました。",
            ephemeral=True,
        )

    @voice_notify_cross_group.command(
        name="invite-remove",
        description="サーバー間通知の招待URLを解除します",
    )
    async def voice_notify_cross_invite_remove(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """サーバー間 VC 入退室通知で使う固定招待 URL を解除する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            config = await get_voice_notify_cross_guild_config(
                session,
                str(interaction.guild_id),
            )
            removed = config is not None and config.invite_url is not None
            if removed:
                await set_voice_notify_cross_guild_invite_url(
                    session,
                    str(interaction.guild_id),
                    None,
                )

        content = (
            "サーバー間VC入退室通知の招待URLを解除しました。"
            if removed
            else "サーバー間VC入退室通知の招待URLは設定されていません。"
        )
        await interaction.response.send_message(content, ephemeral=True)

    @voice_notify_cross_group.command(
        name="receive-remove",
        description="サーバー間VC入退室通知の受信先を解除します",
    )
    async def voice_notify_cross_receive_remove(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """サーバー間 VC 入退室通知の受信先を解除する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            config = await get_voice_notify_cross_guild_config(
                session,
                str(interaction.guild_id),
            )
            removed = config is not None and config.notify_channel_id is not None
            if removed:
                await set_voice_notify_cross_guild_channel(
                    session,
                    str(interaction.guild_id),
                    None,
                )

        content = (
            "サーバー間VC入退室通知の受信先を解除しました。"
            if removed
            else "サーバー間VC入退室通知の受信先は設定されていません。"
        )
        await interaction.response.send_message(content, ephemeral=True)

    @voice_notify_cross_group.command(
        name="status",
        description="サーバー間VC入退室通知の設定を表示します",
    )
    async def voice_notify_cross_status(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """サーバー間 VC 入退室通知設定を表示する。"""
        if not await self._ensure_voice_notify_guild(interaction):
            return

        async with async_session() as session:
            config = await get_voice_notify_cross_guild_config(
                session,
                str(interaction.guild_id),
            )

        if config is None:
            await interaction.response.send_message(
                "サーバー間VC入退室通知は設定されていません。",
                ephemeral=True,
            )
            return

        share_status = "ON" if config.share_enabled else "OFF"
        receive_status = (
            f"<#{config.notify_channel_id}>"
            if config.notify_channel_id is not None
            else "未設定"
        )
        invite_status = "設定済み" if config.invite_url is not None else "未設定"
        await interaction.response.send_message(
            "\n".join(
                [
                    "サーバー間VC入退室通知の設定:",
                    f"共有: {share_status}",
                    f"受信先: {receive_status}",
                    f"招待URL: {invite_status}",
                ]
            ),
            ephemeral=True,
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


class NumberedLobbyModal(discord.ui.Modal):
    """連番共有ロビーを短い入力フォームで作成する Modal。"""

    def __init__(
        self,
        cog: VoiceCog,
        *,
        lobby_name: str,
        room_prefix: str,
        start_number: int,
        number_style: str,
        feature_preset: str,
    ) -> None:
        super().__init__(title="連番ロビー作成")
        self.cog = cog

        number_style_label = "全角" if number_style == NUMBER_STYLE_FULL else "半角"
        feature_preset_label = (
            "人数のみ" if feature_preset == FEATURE_PRESET_LIMIT_ONLY else "全機能"
        )

        self.lobby_name_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="ロビーVC名",
            default=lobby_name,
            placeholder="例: ⌛️もくもく空間作成",
            max_length=100,
        )
        self.room_prefix_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="作成VC名の前半",
            default=room_prefix,
            placeholder="例: ⌛️もくもく空間",
            max_length=95,
        )
        self.start_number_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="開始番号",
            default=str(start_number),
            placeholder="例: 2",
            max_length=10,
        )
        self.number_style_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="数字形式",
            default=number_style_label,
            placeholder="半角 / 全角",
            max_length=10,
        )
        self.feature_preset_input: discord.ui.TextInput[Any] = discord.ui.TextInput(
            label="変更可能な機能",
            default=feature_preset_label,
            placeholder="人数のみ / 全機能",
            max_length=20,
        )

        self.add_item(self.lobby_name_input)
        self.add_item(self.room_prefix_input)
        self.add_item(self.start_number_input)
        self.add_item(self.number_style_input)
        self.add_item(self.feature_preset_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Modal 入力をロビー作成設定に変換して作成する。"""
        lobby_name = str(self.lobby_name_input.value).strip()
        room_prefix = str(self.room_prefix_input.value).strip()
        if not lobby_name:
            await interaction.response.send_message(
                "ロビー名を入力してください。",
                ephemeral=True,
            )
            return
        if not room_prefix:
            await interaction.response.send_message(
                "作成VC名の前半を入力してください。",
                ephemeral=True,
            )
            return

        try:
            start_number = _parse_modal_start_number(self.start_number_input.value)
            number_style = _parse_modal_number_style(self.number_style_input.value)
            feature_preset = _parse_modal_feature_preset(
                self.feature_preset_input.value
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await self.cog._create_lobby_from_config(
            interaction,
            LobbyCreateConfig(
                lobby_name=lobby_name,
                naming_mode=LOBBY_NAMING_NUMBERED,
                room_prefix=room_prefix,
                number_style=number_style,
                number_match_mode=NUMBER_MATCH_BOTH,
                start_number=start_number,
                owner_mode=LOBBY_OWNER_MODE_NONE,
                control_policy=LOBBY_CONTROL_MEMBERS,
                feature_preset=feature_preset,
                default_user_limit=0,
                feature_overrides=_empty_feature_overrides(),
            ),
        )


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
