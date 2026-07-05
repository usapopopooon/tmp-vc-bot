"""VC 入退室通知設定の DB 操作。"""

from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import (
    VoiceNotifyCategoryConfig,
    VoiceNotifyConfig,
    VoiceNotifyCrossGuildConfig,
    VoiceNotifyCrossGuildExclude,
    VoiceNotifyExclude,
)

__all__ = [
    "add_voice_notify_cross_guild_exclude",
    "add_voice_notify_exclude",
    "delete_voice_notify_by_channel",
    "delete_voice_notify_by_guild",
    "delete_voice_notify_category_config",
    "delete_voice_notify_config",
    "delete_voice_notify_cross_guild_config",
    "delete_voice_notify_cross_guild_exclude",
    "delete_voice_notify_exclude",
    "get_voice_notify_cross_guild_config",
    "get_voice_notify_category_config",
    "get_voice_notify_config",
    "is_voice_notify_cross_guild_excluded",
    "is_voice_notify_excluded",
    "list_voice_notify_cross_guild_excludes",
    "list_voice_notify_cross_guild_receivers",
    "list_voice_notify_category_configs",
    "list_voice_notify_configs",
    "list_voice_notify_configs_by_voice_channel",
    "list_voice_notify_excludes",
    "set_voice_notify_category_config",
    "set_voice_notify_config",
    "set_voice_notify_cross_guild_channel",
    "set_voice_notify_cross_guild_invite_url",
    "set_voice_notify_cross_guild_share",
]


def _rowcount(result: Any) -> int:
    """SQLAlchemy の rowcount を int として扱う。"""
    return int(getattr(result, "rowcount", 0) or 0)


async def get_voice_notify_config(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> VoiceNotifyConfig | None:
    """VC 単位の通知設定を取得する。"""
    result = await session.execute(
        select(VoiceNotifyConfig).where(
            VoiceNotifyConfig.guild_id == guild_id,
            VoiceNotifyConfig.voice_channel_id == voice_channel_id,
        )
    )
    return result.scalar_one_or_none()


async def list_voice_notify_configs(
    session: AsyncSession,
    guild_id: str,
) -> list[VoiceNotifyConfig]:
    """サーバー内の VC 単位通知設定を作成順に取得する。"""
    result = await session.execute(
        select(VoiceNotifyConfig)
        .where(VoiceNotifyConfig.guild_id == guild_id)
        .order_by(VoiceNotifyConfig.created_at, VoiceNotifyConfig.id)
    )
    return list(result.scalars().all())


async def list_voice_notify_configs_by_voice_channel(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> list[VoiceNotifyConfig]:
    """指定 VC に紐づく通知設定を取得する。"""
    result = await session.execute(
        select(VoiceNotifyConfig).where(
            VoiceNotifyConfig.guild_id == guild_id,
            VoiceNotifyConfig.voice_channel_id == voice_channel_id,
        )
    )
    return list(result.scalars().all())


async def set_voice_notify_config(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
    notify_channel_id: str,
) -> VoiceNotifyConfig:
    """VC 単位の通知設定を作成または更新する。"""
    config = await get_voice_notify_config(session, guild_id, voice_channel_id)
    if config is None:
        config = VoiceNotifyConfig(
            guild_id=guild_id,
            voice_channel_id=voice_channel_id,
            notify_channel_id=notify_channel_id,
        )
        session.add(config)
    else:
        config.notify_channel_id = notify_channel_id

    await session.commit()
    await session.refresh(config)
    return config


async def delete_voice_notify_config(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> bool:
    """VC 単位の通知設定を削除する。"""
    result = await session.execute(
        delete(VoiceNotifyConfig).where(
            VoiceNotifyConfig.guild_id == guild_id,
            VoiceNotifyConfig.voice_channel_id == voice_channel_id,
        )
    )
    await session.commit()
    return _rowcount(result) > 0


async def get_voice_notify_category_config(
    session: AsyncSession,
    guild_id: str,
    category_id: str,
) -> VoiceNotifyCategoryConfig | None:
    """カテゴリ単位の通知設定を取得する。"""
    result = await session.execute(
        select(VoiceNotifyCategoryConfig).where(
            VoiceNotifyCategoryConfig.guild_id == guild_id,
            VoiceNotifyCategoryConfig.category_id == category_id,
        )
    )
    return result.scalar_one_or_none()


async def list_voice_notify_category_configs(
    session: AsyncSession,
    guild_id: str,
) -> list[VoiceNotifyCategoryConfig]:
    """サーバー内のカテゴリ単位通知設定を作成順に取得する。"""
    result = await session.execute(
        select(VoiceNotifyCategoryConfig)
        .where(VoiceNotifyCategoryConfig.guild_id == guild_id)
        .order_by(VoiceNotifyCategoryConfig.created_at, VoiceNotifyCategoryConfig.id)
    )
    return list(result.scalars().all())


async def set_voice_notify_category_config(
    session: AsyncSession,
    guild_id: str,
    category_id: str,
    notify_channel_id: str,
) -> VoiceNotifyCategoryConfig:
    """カテゴリ単位の通知設定を作成または更新する。"""
    config = await get_voice_notify_category_config(session, guild_id, category_id)
    if config is None:
        config = VoiceNotifyCategoryConfig(
            guild_id=guild_id,
            category_id=category_id,
            notify_channel_id=notify_channel_id,
        )
        session.add(config)
    else:
        config.notify_channel_id = notify_channel_id

    await session.commit()
    await session.refresh(config)
    return config


async def delete_voice_notify_category_config(
    session: AsyncSession,
    guild_id: str,
    category_id: str,
) -> bool:
    """カテゴリ単位の通知設定を削除する。"""
    result = await session.execute(
        delete(VoiceNotifyCategoryConfig).where(
            VoiceNotifyCategoryConfig.guild_id == guild_id,
            VoiceNotifyCategoryConfig.category_id == category_id,
        )
    )
    await session.commit()
    return _rowcount(result) > 0


async def list_voice_notify_excludes(
    session: AsyncSession,
    guild_id: str,
) -> list[VoiceNotifyExclude]:
    """サーバー内のカテゴリ通知除外 VC を作成順に取得する。"""
    result = await session.execute(
        select(VoiceNotifyExclude)
        .where(VoiceNotifyExclude.guild_id == guild_id)
        .order_by(VoiceNotifyExclude.created_at, VoiceNotifyExclude.id)
    )
    return list(result.scalars().all())


async def is_voice_notify_excluded(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> bool:
    """指定 VC がカテゴリ通知の除外対象かを返す。"""
    result = await session.execute(
        select(VoiceNotifyExclude.id).where(
            VoiceNotifyExclude.guild_id == guild_id,
            VoiceNotifyExclude.voice_channel_id == voice_channel_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def add_voice_notify_exclude(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> VoiceNotifyExclude:
    """カテゴリ通知の除外 VC を追加する。既存ならそのまま返す。"""
    result = await session.execute(
        select(VoiceNotifyExclude).where(
            VoiceNotifyExclude.guild_id == guild_id,
            VoiceNotifyExclude.voice_channel_id == voice_channel_id,
        )
    )
    exclude = result.scalar_one_or_none()
    if exclude is None:
        exclude = VoiceNotifyExclude(
            guild_id=guild_id,
            voice_channel_id=voice_channel_id,
        )
        session.add(exclude)
        await session.commit()
        await session.refresh(exclude)
    return exclude


async def delete_voice_notify_exclude(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> bool:
    """カテゴリ通知の除外 VC を削除する。"""
    result = await session.execute(
        delete(VoiceNotifyExclude).where(
            VoiceNotifyExclude.guild_id == guild_id,
            VoiceNotifyExclude.voice_channel_id == voice_channel_id,
        )
    )
    await session.commit()
    return _rowcount(result) > 0


async def get_voice_notify_cross_guild_config(
    session: AsyncSession,
    guild_id: str,
) -> VoiceNotifyCrossGuildConfig | None:
    """サーバー間 VC 通知設定を取得する。"""
    result = await session.execute(
        select(VoiceNotifyCrossGuildConfig).where(
            VoiceNotifyCrossGuildConfig.guild_id == guild_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_or_create_voice_notify_cross_guild_config(
    session: AsyncSession,
    guild_id: str,
) -> VoiceNotifyCrossGuildConfig:
    """サーバー間 VC 通知設定を取得し、なければ作成する。"""
    config = await get_voice_notify_cross_guild_config(session, guild_id)
    if config is None:
        config = VoiceNotifyCrossGuildConfig(guild_id=guild_id)
        session.add(config)
    return config


async def set_voice_notify_cross_guild_share(
    session: AsyncSession,
    guild_id: str,
    enabled: bool,
) -> VoiceNotifyCrossGuildConfig:
    """このサーバーの VC 入退室を他サーバーへ共有するか設定する。"""
    config = await _get_or_create_voice_notify_cross_guild_config(session, guild_id)
    config.share_enabled = enabled

    await session.commit()
    await session.refresh(config)
    return config


async def set_voice_notify_cross_guild_channel(
    session: AsyncSession,
    guild_id: str,
    notify_channel_id: str | None,
) -> VoiceNotifyCrossGuildConfig:
    """他サーバーから共有された VC 入退室通知の受信先を設定する。"""
    config = await _get_or_create_voice_notify_cross_guild_config(session, guild_id)
    config.notify_channel_id = notify_channel_id

    await session.commit()
    await session.refresh(config)
    return config


async def set_voice_notify_cross_guild_invite_url(
    session: AsyncSession,
    guild_id: str,
    invite_url: str | None,
) -> VoiceNotifyCrossGuildConfig:
    """サーバー間 VC 通知のリンクに使う固定招待 URL を設定する。"""
    config = await _get_or_create_voice_notify_cross_guild_config(session, guild_id)
    config.invite_url = invite_url

    await session.commit()
    await session.refresh(config)
    return config


async def list_voice_notify_cross_guild_receivers(
    session: AsyncSession,
    *,
    exclude_guild_id: str | None = None,
) -> list[VoiceNotifyCrossGuildConfig]:
    """サーバー間 VC 通知の受信先がある設定を取得する。"""
    statement = select(VoiceNotifyCrossGuildConfig).where(
        VoiceNotifyCrossGuildConfig.notify_channel_id.is_not(None),
    )
    if exclude_guild_id is not None:
        statement = statement.where(
            VoiceNotifyCrossGuildConfig.guild_id != exclude_guild_id,
        )
    statement = statement.order_by(
        VoiceNotifyCrossGuildConfig.created_at,
        VoiceNotifyCrossGuildConfig.id,
    )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def list_voice_notify_cross_guild_excludes(
    session: AsyncSession,
    guild_id: str,
) -> list[VoiceNotifyCrossGuildExclude]:
    """サーバー間通知の除外 VC を作成順に取得する。"""
    result = await session.execute(
        select(VoiceNotifyCrossGuildExclude)
        .where(VoiceNotifyCrossGuildExclude.guild_id == guild_id)
        .order_by(
            VoiceNotifyCrossGuildExclude.created_at,
            VoiceNotifyCrossGuildExclude.id,
        )
    )
    return list(result.scalars().all())


async def is_voice_notify_cross_guild_excluded(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> bool:
    """指定 VC がサーバー間通知の除外対象かを返す。"""
    result = await session.execute(
        select(VoiceNotifyCrossGuildExclude.id).where(
            VoiceNotifyCrossGuildExclude.guild_id == guild_id,
            VoiceNotifyCrossGuildExclude.voice_channel_id == voice_channel_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def add_voice_notify_cross_guild_exclude(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> VoiceNotifyCrossGuildExclude:
    """サーバー間通知の除外 VC を追加する。既存ならそのまま返す。"""
    result = await session.execute(
        select(VoiceNotifyCrossGuildExclude).where(
            VoiceNotifyCrossGuildExclude.guild_id == guild_id,
            VoiceNotifyCrossGuildExclude.voice_channel_id == voice_channel_id,
        )
    )
    exclude = result.scalar_one_or_none()
    if exclude is None:
        exclude = VoiceNotifyCrossGuildExclude(
            guild_id=guild_id,
            voice_channel_id=voice_channel_id,
        )
        session.add(exclude)
        await session.commit()
        await session.refresh(exclude)
    return exclude


async def delete_voice_notify_cross_guild_exclude(
    session: AsyncSession,
    guild_id: str,
    voice_channel_id: str,
) -> bool:
    """サーバー間通知の除外 VC を削除する。"""
    result = await session.execute(
        delete(VoiceNotifyCrossGuildExclude).where(
            VoiceNotifyCrossGuildExclude.guild_id == guild_id,
            VoiceNotifyCrossGuildExclude.voice_channel_id == voice_channel_id,
        )
    )
    await session.commit()
    return _rowcount(result) > 0


async def delete_voice_notify_cross_guild_config(
    session: AsyncSession,
    guild_id: str,
) -> bool:
    """サーバー間 VC 通知設定を削除する。"""
    result = await session.execute(
        delete(VoiceNotifyCrossGuildConfig).where(
            VoiceNotifyCrossGuildConfig.guild_id == guild_id,
        )
    )
    await session.commit()
    return _rowcount(result) > 0


async def delete_voice_notify_by_guild(
    session: AsyncSession,
    guild_id: str,
) -> int:
    """指定サーバーの通知設定を全て削除する。"""
    voice_result = await session.execute(
        delete(VoiceNotifyConfig).where(VoiceNotifyConfig.guild_id == guild_id)
    )
    category_result = await session.execute(
        delete(VoiceNotifyCategoryConfig).where(
            VoiceNotifyCategoryConfig.guild_id == guild_id
        )
    )
    exclude_result = await session.execute(
        delete(VoiceNotifyExclude).where(VoiceNotifyExclude.guild_id == guild_id)
    )
    cross_exclude_result = await session.execute(
        delete(VoiceNotifyCrossGuildExclude).where(
            VoiceNotifyCrossGuildExclude.guild_id == guild_id
        )
    )
    cross_result = await session.execute(
        delete(VoiceNotifyCrossGuildConfig).where(
            VoiceNotifyCrossGuildConfig.guild_id == guild_id
        )
    )
    await session.commit()
    return (
        _rowcount(voice_result)
        + _rowcount(category_result)
        + _rowcount(exclude_result)
        + _rowcount(cross_exclude_result)
        + _rowcount(cross_result)
    )


async def delete_voice_notify_by_channel(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
) -> int:
    """削除されたチャンネルに関係する通知設定を削除する。"""
    voice_result = await session.execute(
        delete(VoiceNotifyConfig).where(
            VoiceNotifyConfig.guild_id == guild_id,
            or_(
                VoiceNotifyConfig.voice_channel_id == channel_id,
                VoiceNotifyConfig.notify_channel_id == channel_id,
            ),
        )
    )
    category_result = await session.execute(
        delete(VoiceNotifyCategoryConfig).where(
            VoiceNotifyCategoryConfig.guild_id == guild_id,
            or_(
                VoiceNotifyCategoryConfig.category_id == channel_id,
                VoiceNotifyCategoryConfig.notify_channel_id == channel_id,
            ),
        )
    )
    exclude_result = await session.execute(
        delete(VoiceNotifyExclude).where(
            VoiceNotifyExclude.guild_id == guild_id,
            VoiceNotifyExclude.voice_channel_id == channel_id,
        )
    )
    cross_exclude_result = await session.execute(
        delete(VoiceNotifyCrossGuildExclude).where(
            VoiceNotifyCrossGuildExclude.guild_id == guild_id,
            VoiceNotifyCrossGuildExclude.voice_channel_id == channel_id,
        )
    )
    cross_result = await session.execute(
        update(VoiceNotifyCrossGuildConfig)
        .where(
            VoiceNotifyCrossGuildConfig.guild_id == guild_id,
            VoiceNotifyCrossGuildConfig.notify_channel_id == channel_id,
        )
        .values(notify_channel_id=None)
    )
    await session.commit()
    return (
        _rowcount(voice_result)
        + _rowcount(category_result)
        + _rowcount(exclude_result)
        + _rowcount(cross_exclude_result)
        + _rowcount(cross_result)
    )
