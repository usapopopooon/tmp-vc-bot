"""一時 VC の表示権限を安全に切り替える処理。"""

from __future__ import annotations

import discord

from src.database.models import VoiceSession

_ROLE_KEY_PREFIX = "role:"
_MEMBER_KEY_PREFIX = "member:"


def _copy_overwrite(
    overwrite: discord.PermissionOverwrite | None,
) -> discord.PermissionOverwrite:
    """PermissionOverwrite を複製する。"""
    if not isinstance(overwrite, discord.PermissionOverwrite):
        return discord.PermissionOverwrite()
    allow, deny = overwrite.pair()
    return discord.PermissionOverwrite.from_pair(allow, deny)


def _target_key(target: discord.Member | discord.Role) -> str:
    """権限対象を永続化用の一意なキーに変換する。"""
    prefix = (
        _ROLE_KEY_PREFIX if isinstance(target, discord.Role) else _MEMBER_KEY_PREFIX
    )
    return f"{prefix}{target.id}"


def _stored_snapshot(voice_session: VoiceSession) -> dict[str, bool | None] | None:
    """DB に保存済みの表示権限スナップショットを返す。"""
    value = getattr(voice_session, "hidden_view_overwrites", None)
    if not isinstance(value, dict):
        return None
    return {
        key: permission
        for key, permission in value.items()
        if isinstance(key, str) and permission in (True, False, None)
    }


def _direct_view_permission(
    channel: discord.VoiceChannel,
    target: discord.Member | discord.Role,
) -> bool | None:
    """対象に直接設定された view_channel の値だけを返す。"""
    overwrite = channel.overwrites_for(target)
    if not isinstance(overwrite, discord.PermissionOverwrite):
        return None
    return overwrite.view_channel


async def _set_view_permission(
    channel: discord.VoiceChannel,
    target: discord.Member | discord.Role,
    value: bool | None,
) -> None:
    """他の権限を維持して view_channel だけを変更する。"""
    overwrite = _copy_overwrite(channel.overwrites_for(target))
    overwrite.update(view_channel=value)
    if overwrite.is_empty():
        await channel.set_permissions(target, overwrite=None)
    else:
        await channel.set_permissions(target, overwrite=overwrite)


def _remember_permission(
    snapshot: dict[str, bool | None],
    target: discord.Member | discord.Role,
    current: bool | None,
    *,
    legacy_hidden_session: bool,
    default_role: discord.Role,
) -> None:
    """非表示処理前の値を、同じ対象につき一度だけ保存する。"""
    key = _target_key(target)
    if key in snapshot:
        return

    # この列がない旧バージョンで非表示にされたセッションでは、@everyone と
    # 個別メンバーの view_channel は Bot が追加した値として扱う。
    if legacy_hidden_session and (
        isinstance(target, discord.Member) or target.id == default_role.id
    ):
        snapshot[key] = None
    else:
        snapshot[key] = current


async def hide_voice_channel(
    channel: discord.VoiceChannel,
    voice_session: VoiceSession,
) -> None:
    """管理者以外は在室者だけが VC を見られる権限状態にする。

    @everyone の拒否より後にロール・メンバーの許可が適用される Discord の
    権限解決順序を考慮し、漏れにつながる明示的な許可も拒否へ切り替える。
    元の値は VoiceSession に保存し、表示へ戻す際に復元する。
    """
    stored = _stored_snapshot(voice_session)
    legacy_hidden_session = voice_session.is_hidden and stored is None
    snapshot = dict(stored or {})
    guild = channel.guild
    default_role = guild.default_role

    current_default = _direct_view_permission(channel, default_role)
    _remember_permission(
        snapshot,
        default_role,
        current_default,
        legacy_hidden_session=legacy_hidden_session,
        default_role=default_role,
    )
    if current_default is not False:
        await _set_view_permission(channel, default_role, False)

    allowed_members = {member.id: member for member in channel.members}
    bot_member = guild.me
    if isinstance(bot_member, discord.Member):
        # Bot 自身がパネル更新やチャンネル管理を続けられるようにする。
        allowed_members[bot_member.id] = bot_member

    # スナップショットを取ってから API 更新を行うため、反復元は固定する。
    for target, overwrite in list(channel.overwrites.items()):
        if target.id == default_role.id:
            continue
        direct = (
            overwrite.view_channel
            if isinstance(overwrite, discord.PermissionOverwrite)
            else _direct_view_permission(channel, target)
        )
        if isinstance(target, discord.Role):
            should_deny = True
        elif isinstance(target, discord.Member):
            # 同じサーバーを複数 Bot で管理しても互いの個別許可を潰さない。
            should_deny = target.id not in allowed_members and not target.bot
        else:
            continue
        if not should_deny or direct is not True:
            continue
        _remember_permission(
            snapshot,
            target,
            direct,
            legacy_hidden_session=legacy_hidden_session,
            default_role=default_role,
        )
        await _set_view_permission(channel, target, False)

    for member in allowed_members.values():
        current = _direct_view_permission(channel, member)
        _remember_permission(
            snapshot,
            member,
            current,
            legacy_hidden_session=legacy_hidden_session,
            default_role=default_role,
        )
        if current is not True:
            await _set_view_permission(channel, member, True)

    voice_session.hidden_view_overwrites = snapshot


def _resolve_target(
    guild: discord.Guild,
    key: str,
) -> discord.Member | discord.Role | None:
    """永続化したキーから現在の Discord 権限対象を取得する。"""
    prefix, separator, raw_id = key.partition(":")
    if not separator or not raw_id.isdigit():
        return None
    target_id = int(raw_id)
    if prefix == _ROLE_KEY_PREFIX[:-1]:
        if target_id == guild.default_role.id:
            return guild.default_role
        return guild.get_role(target_id)
    if prefix == _MEMBER_KEY_PREFIX[:-1]:
        return guild.get_member(target_id)
    return None


async def show_voice_channel(
    channel: discord.VoiceChannel,
    voice_session: VoiceSession,
) -> None:
    """非表示前に保存した view_channel の値を対象ごとに復元する。"""
    snapshot = _stored_snapshot(voice_session)
    if snapshot is not None:
        for key, original in snapshot.items():
            target = _resolve_target(channel.guild, key)
            if target is None:
                continue
            if _direct_view_permission(channel, target) is original:
                continue
            await _set_view_permission(channel, target, original)
    else:
        # 旧バージョンで非表示にされたセッションの後方互換。
        await _set_view_permission(channel, channel.guild.default_role, None)
        for overwrite_target, overwrite in list(channel.overwrites.items()):
            if (
                isinstance(overwrite_target, discord.Member)
                and isinstance(overwrite, discord.PermissionOverwrite)
                and overwrite.view_channel is True
            ):
                await _set_view_permission(channel, overwrite_target, None)

    voice_session.hidden_view_overwrites = None


async def set_hidden_member_visibility(
    channel: discord.VoiceChannel,
    voice_session: VoiceSession,
    member: discord.Member,
    *,
    visible: bool,
) -> None:
    """非表示 VC への入退室に合わせて個別の表示権限を更新する。"""
    if not voice_session.is_hidden:
        return

    stored = _stored_snapshot(voice_session)
    snapshot = dict(stored or {})
    current = _direct_view_permission(channel, member)
    _remember_permission(
        snapshot,
        member,
        current,
        legacy_hidden_session=stored is None,
        default_role=channel.guild.default_role,
    )
    desired = visible
    if current is not desired:
        await _set_view_permission(channel, member, desired)
    voice_session.hidden_view_overwrites = snapshot
